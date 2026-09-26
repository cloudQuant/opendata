# C29 轮：前端单元面的静默排除——「8 文件 / 79 用例」是 collector 允许的大小，不是面的大小（task #37）

日期：2026-09-26｜分支：`dev`｜前置：C28（v4.9，`92f8847`）

验收文档里「前端 8 文件 / 79 用例」这句话出现了 **16 次**。本轮量的就是这一句：
它不是这个面的大小，而是 `frontend/vite.config.ts` 的 `test.exclude` **允许**它的大小。
真面是 `src/` 下 **13 个文件 / 99 例**，另有 e2e 面 3 文件 / 21 例属另一个 runner（那是**声明的边界**，
本轮没有把它们拖进 src 面冒充扩大）。差的那 **5 个文件 / 18 例**从 A0 起被一条
`exclude: ['src/components/common/__tests__/**']` 挡在外面，而挡住它们的规则就写在这条门禁自己的配置文件里。
一次 run 的绿色不能报告它没跑过的东西：`Test Files 8 passed (8)` 与「盘上有 13 个 src 测试文件」完全兼容。

下面这些读数是本轮真正的交付：**18 例里 16 例本来就该跑**（解除静默后只挂 2 例，两处都是测试侧坏死）、
**一条门禁项在自己烂掉时照样绿**（三条规则被逐个拆掉，`frontend-collection` 三次都 rc=0），
以及判定面因此被搬进单测（10 例）。

---

## 1. 缺口为什么只能从外面看见

`make gate` 的 `frontend-test` 项跑 `npx vitest run`。vitest 不会为**被它自己的 exclude 排除的文件**出声：
它报的是它跑过的东西。所以本轮之前所有门禁留档里的「8 files / 79 tests」都是**同一个数字**，
而那条 exclude 与门禁的其他 7 条 exclude 出自同一个 commit（`4ee1bbc 2026-09-22 chore(a0)`）——
没有任何一轮是「测出需要它」之后才把它加上的。

`test-plane-census-before.txt`（独立实现，不复用守卫的任何代码）的读数：

| 读数 | 值 |
|------|----|
| 盘上测试文件 / vitest 采集到的 | **16 / 8** |
| 盘上用例位 / 采集到的用例位 | **118 / 79** |
| 其中 src 面（门禁盲区） | **5 文件 / 18 例** |
| 其中 e2e 面（另一个 runner） | 3 文件 / 21 例 |
| 声明的 exclude 归属 | `test` 4 条 / `coverage` 4 条 |

对账闭合：`118 = 79（跑了）+ 18（被挡）+ 21（e2e）`，`120 = 99 + 21`（修复后）。
而 census 的**静态** `it(`/`test(` 计数与 vitest 的**运行时**报数在两个状态下都逐字相等（79=79、99=99）
⇒ 那 18 例是真实用例位，不是计数口径造出来的差。

## 2. 测量面自己先犯了它要量的错（两处，都会把结论读反）

1. **把 `coverage.exclude` 当成 collector 静默**。census 第一版把配置里所有 `exclude: [...]` 当一个数组读，
   于是报出 8 条「静默规则」，其中 4 条其实是 coverage 的（`src/**/*.d.ts`、`src/main.ts`…）。
   `coverage.exclude` 只缩分母，**不挡运行**——这正是本轮判据的核心区分，测量脚本自己先撞上它。
   改成按 owning object 归属（`owned by test: 4` / `owned by coverage: 4`）。
2. **把生成物当成使用点**。`src/components.d.ts`（unplugin 自动生成的全局组件声明）和 barrel
   `components/common/index.ts` 被算作「引用这 5 个组件的文件」，读起来像「这些组件都在用」。
   排除生成物后的真读数：`StatCard` 1 处使用（`src/views/HomeView.vue`），其余 4 个 **0 处**。

两处都在 before 档的表头里写明自纠过程，**没有改写档内上面任何一行读数**。

## 3. 解除静默后立刻挂 2 例：两条都是「测试读自己的夹具」

首跑 13 文件 / 97 例，2 条红。逐条归因后才动手（判据：改的是**断言对象**还是**期望值**）。

| 文件 | 挂在哪 | 为什么是测试侧坏死而不是产品回归 | 处置 |
|------|--------|--------------------------------|------|
| `StatCard.test.ts` | `wrapper.find('[data-testid="stat-card"]')` | 那个属性是它自己的 `ElCardStub` 注入的。`unplugin-vue-components` 把 `<el-card>` 编译成**直接 import**，按 kebab 名注册进 `global.components` 的桩永远不会被用到 ⇒ 这条断言读的是**测试自己写下的标记**，产品做错任何事都不会让它红 | 改成对真挂载输出断言（`.stat-card` 存在且含 `hoverable`、`.stat-value` = `123`、`.stat-label` = `Views`），再补 `hoverable: false` 与 icon 有/无两面：**1 例 → 3 例** |
| `FilterBar.test.ts` | `input.vm.handleClear()` | 装的 Element Plus 上没有这个私有方法 ⇒ 它**只能挂**；而它从写下起就挂在 collector 排除后面，所以「只能挂」这件事从未被执行过 | 改测契约而非私有方法：`ElInput` 发 `clear` ⇒ 组件同时 emit `clear` **且** 把 `update:searchValue` 置为 `''` |
| `EmptyState` / `PageHeader` / `StatusBadge` | 不挂 | 断言落在真实渲染文本与类名上（`暂无数据`、`.page-header.bordered`、`等待中/已完成`） | **一字未动**，5+4+3 例直接进面 |

⇒ 用例数变化：`79 + 18 − 1 + 3 = 99`（其中 −1 是 StatCard 原来的那 1 例被 3 例替换），
净 +20 里只有 +2 是本轮新写的判断，另外 18 例一直在盘上。

## 4. 判定面：三条规则，外加「读不出来」不算通过

`scripts/quality/frontend_test_collection.py`（同一份 `judge()` 被门禁项与注入巡回共用，判定面唯一）：

| 规则 | 管什么 | 别的规则管不到什么 |
|------|--------|--------------------|
| `SILENCED` | 盘上有、采集里没有的文件，并点名是哪条 exclude 干的 | 看不见「还没有文件被挡」的规则 |
| `UNAUTHORISED` | exclude 里出现白名单之外的任何 glob | 一条新规则刚写下的那天**还没挡住任何文件**，只有这条能抓到它 |
| `VACUOUS` ⇒ **exit 2** | 配置读不出 `test.exclude`、`npx vitest list` 不答、src 下一个测试文件都没有 | 前两条会把这些情况读成「什么都没有 ⇒ 通过」 |

两条设计决定是量出来才写下的：

- **allow-list 而不是「探针路径」**。规则 2 的第一版只认「排除的是我本轮的探针文件」，
  于是 `src/legacy/**` 这种形状能直接溜过去。改成 `{e2e/**, node_modules/**, dist/**}` 白名单后，
  新增一条静默是一个**要写理由、要改判定面**的动作，不是一个能过 gate 的 diff。
  白名单三条各有语义：e2e = 独立 runner，`node_modules`/`dist` = 不是源码——所以 e2e 那 21 例
  是声明的边界而不是缺陷，`e2e/**` 两条规则都不认它是静默。
- **读不出来必须红**。守卫第一版对真配置返回的就是 VACUOUS（rc=2）：它按缩进切 `test:` 块，
  而 `vite.config.ts` 的嵌套缩进不是它猜的那个。方向是刻意的（fail-closed，宁可承认没量到），
  换成 `object_block()` 括号配对 + 在嵌套 `coverage:` 处切断之后，这个方向保留不变。

## 5. 四面证明它能红——以及本轮最贵的一条读数：门禁项自己看不见自己烂掉

| 证明 | 对象 | 读数 |
|------|------|------|
| ① 真树 before | 本轮改动前的配置（对 `git show HEAD:` 的字节跑守卫后按字节还原） | `GUARD_RC=1`：5 个文件名逐个 `SILENCED` + 1 条 `UNAUTHORISED`，`config bytes restored: YES`、`worktree unchanged: YES`（`git status` sha256 before==after `770786b466f8`） |
| ② 注入巡回 | 纯函数 `judge()` / `parse_test_excludes()`，7 judge + 3 config 形状 | `MISMATCHES: 0`、`HARNESS_RC=0`；含两条**假阳性守卫**（e2e 边界、node_modules/dist 必须判 PASS）与「coverage 条目泄漏进 collector 规则：none」 |
| ③ 真树可逆变异 | 门禁项实际跑的那条命令 + 真的 `frontend/` 树 | step0 `rc=0 counts=(13,13)` → step1 加一个探针文件 `rc=0 counts=(14,14)`（守卫跟着它动）→ step2 按 A0 的方式把它单独排除 `rc=1`，`SILENCED` 与 `UNAUTHORISED` **同时点名它**；探针删除、字节还原、`PROBLEMS: 0` |
| ④ **规则拆除** | 守卫模块本身（4 个变异，按字节还原 + sha256 `6bd6b15c3838` 复核） | 见下表 |

④ 这张表是本轮最重要的一条：

| 变异 | 拆掉的规则 | 门禁项 `frontend-collection` | 新增的 pytest 面 |
|------|-----------|------------------------------|------------------|
| `silence_rule_dead` | SILENCED 的循环对象 | **rc=0，照打 PASS** | 2 例红 |
| `allow_list_dead` | 白名单比较 | **rc=0，照打 PASS** | 2 例红（其一与上一行共享那条「reasons 恰为 2」的计数断言） |
| `vacuity_dead` | `vacuous=not on_disk` | **rc=0，照打 PASS** | 1 例红 |
| `reader_dead` | 配置读取 | rc=2（VACUOUS，fail-closed 兑现） | 2 例红 |

⇒ **门禁项判的是「今天的树」，而今天的树确实没有静默**，所以前三条规则被逐个拆掉它三次都绿。
把守卫接进 gate 只解决了「没人跑它」，没解决「它自己烂掉」——所以判定规则同时活在
`tests/test_frontend_collection_guard.py`（10 例）里，其中一条直接对**出厂配置**取证：
`set(globs) == set(ALLOWED_EXCLUDES)`，即任何人把 `src/` 的排除加回去，Python 面立刻红，
不需要等到真的有文件被挡。四个变异全部还原，还原后两面都回绿。

## 6. 接线

- `Makefile`：`frontend-collection` 进 `.PHONY`、进 `help`、进 `gate`（跑在 `frontend-test` **之前**）、
  进 `quality-full` 别名。顺序理由是失败文本的可用性：先说「面是全的」再说「面是绿的」，
  反过来一次用例会先给出断言错误，把静默这条线索埋掉。
- `frontend/vite.config.ts`：删掉那条 exclude，并留一句注释指向守卫脚本（防「顺手加回去」）。
  本轮**没有**改用 `include`——`include` 的默认值本身就能藏文件，且藏得更难看出来（§7-1）。

## 7. 明确拒绝的六种「让它过去」

1. **用 `include` 白名单替换 `exclude`**。看着更严格，但一条写漏的 pattern 同样能藏文件，
   而这次连「盘上有 / 没采集」这个外部对照都不会再提。本轮要的是对照，不是换个写法。
2. **把 e2e 那 3 文件 21 例算进本轮的用例增量**。它们属 playwright，需要浏览器与后端。
   本轮把它做成 `ALLOWED_EXCLUDES` 里一条有名有姓的边界，并登记为遗留（§11-1），没有并入。
3. **给 `judge()` 补几条空断言用例来凑 Python 覆盖率**。新增 10 例每条都能说出「拆掉哪条规则它红」
   （§5 表），没有一条是「为了走到的行」。
4. **把守卫挪进 `tests/`**（那样它的行就进分母了）。`coverage.source = [opendata, opendata_fuyao]`，
   `scripts/` 本来就不在这个分母里（`a2_check.py`、`scan_js_points.py` 同口径），
   本轮为此**明写**：守卫自身 0 行计入 Python 覆盖率面（§9-3），不去改分母冒充扩大。
5. **上调前端 coverage 阈值冒充测试面扩大**。`thresholds`（60/60/50/60）一字未动，
   且它今天只在 `npm run test:coverage` 下评估、不在 gate 里（§11-4 登记为未收口）。
6. **留着 StatCard 那条 stub 断言**（改完 3 例之后它仍然是绿的）。留着就等于本轮的靶子还在，
   而它正是「看起来有测试」的那一类；已删除并把为什么必须删写进文件注释。

## 8. 证据清单

| 文件 | 内容 | 判据 / 退出码 |
|------|------|-------------|
| `test-plane-census.py` / `test-plane-census-before.txt` / `-after.txt` | 独立测量面（**不 import 守卫**）：逐文件用例位、NEVER COLLECTED 按 src/e2e 分面、exclude 按 owning object 归属 + `git log -S` 出处、被静默组件的使用点 | before `CENSUS_RC=0`（16/8、118/79、src 面 5/18）；after `CENSUS_RC=0`（16/13、120/99、**src 面 0/0**，只剩 e2e 面 3/21） |
| `guard-before.txt` | 对**本轮改动前**的配置树跑出厂守卫（跑完按字节还原） | `GUARD_RC=1`，5 条 `SILENCED` + 1 条 `UNAUTHORISED`；表头说明这是对 before-state 的**第二次采集**（第一次是在规则 2 还是「探针路径」时抓的，与出厂规则不同源） |
| `guard-falsification.py` / `.txt` | 纯函数注入：7 judge 形状 + 3 config 形状，含假阳性守卫与分母泄漏检查 | `MISMATCHES: 0`、`HARNESS_RC=0`；形状里没有任何一条能触发某规则时巡回自己 `exit=2` |
| `real-tree-probe.py` / `.txt` | 真树可逆变异：加文件→按名排除→还原，并复核 `git status` 哈希 | step0 (13,13)/rc0 → step1 (14,14)/rc0 → step2 rc1 双规则点名；`config bytes restored YES`、`probe file gone YES`、`worktree unchanged YES`、`PROBLEMS: 0` |
| `guard-teeth-mutation.py` / `.txt` | 四条规则拆除变异，**同时读门禁项与 pytest 面**（§5 的表） | 4/4 `CAUGHT`、`PROBLEMS: 0`、`TEETH_RC=0`；还原 sha256 一致、restored 两面 rc=0 |
| `plane-after.txt` | 修复后的三面同框：守卫 / 全量 vitest / eslint / vue-tsc，逐条记退出码 | `GUARD_RC=0` 13/13、`VITEST_RC=0` **13 文件 / 99 例**、`ESLINT_RC=0`（0 errors / 46 warnings）、`VUE_TSC_RC=0` |
| `scripts/quality/frontend_test_collection.py` | 出厂判定面（6 个公共函数 + `Verdict.ok`） | 进 `make gate`；`GATE_EXIT=0` |
| `tests/test_frontend_collection_guard.py` | 判定规则的 10 例（含对出厂配置直接取证那一条） | 门禁内跑，+10 与用例增量完全对上 |
| `gate-run1-before-guard-tests.txt` | **被替换的前跑**（不含那 10 例，2,763 passed / a2-check 281 文件） | `GATE_EXIT=0`；档尾补记了它为什么不再是最终判定面，未改写档内任何一行 |
| `gate.txt` | 本轮最终判定面 | `GATE_EXIT=0`（**6,277 行完整未裁剪**，末行读数） |

## 9. 判据未放宽的声明

1. **没有为了过门禁改过任何一条期望值**。§3 那两处改的是断言**对象**（从桩的标记改到真挂载输出、
   从私有方法改到组件契约），改后每个文件用例数只增不减（StatCard 1→3、FilterBar 5→5、其余三个原样进面）。
2. **没有新增任何豁免**：`pyproject.toml`、`bandit.yaml`、`docs/quality/`、`scripts/quality/` 其余文件
   的 diff 为空；`vite.config.ts` 里只删了一条 exclude（不是把它改名或换成 `include`）。
3. **本轮不声称任何覆盖率改善**：`TOTAL` 语句 **10,942 一字未动**（守卫不在分母里），
   最终 86.72% / 未覆 1,285 / 部分 305 与 C28 **逐字相等**；本轮两跑之间的 2 行差额
   100% 落在 `opendata/api/data.py`（run1 未覆 2/部分 2、run2 未覆 4/部分 3），
   198 行覆盖表里 197 行逐字不变 ⇒ 这是 C27 §10-7、C28 §11 登记的那条**时序依赖观测第三次被同一轮两跑抓到**，
   不是本轮工作的功劳，也不是本轮引入的回归。
4. **`6 skipped` 与 C28 逐字同批**（`tests/test_port_fidelity.py` 的 em 那批夹具），本轮一条都没动过它。
5. **历史读数不改写**：§0 是修订记录，前 16 处「8 文件 / 79 用例」原样留在档内；
   本轮只在 v5.0 的行与 AC-17 里登记「那些读数描述的是一个部分静默的面」这一事实。
   同理，`guard-before.txt` 的第一次采集没有被删掉——它换了采集方式重跑，档头写清了为什么。
6. **eslint 46 warnings（C28 为 48）不是「本轮清理了 2 条告警」**：−2 全部来自被删掉的 `ElCardStub`
   （`VueWrapper` 未用 + `vue/require-default-prop`），已用 `npx eslint --stdin` 对 HEAD 版本逐文件复核；
   本轮**没有引入新告警**，也没有为它加 `// eslint-disable`。

## 10. 门禁与测试数字

| 面 | C28 基线 | C29 第 1 跑（无守卫单测） | **C29 最终** |
|----|---------|--------------------------|--------------|
| `make gate` | `GATE_EXIT=0`（6,239 行） | `GATE_EXIT=0`（6,254 行） | **`GATE_EXIT=0`**（`gate.txt` **6,277 行**，末行读数） |
| `pytest -n 8 -m "not e2e"` | 2,763 passed / 6 skipped（51.15s） | 2,763 / 6（49.97s） | **2,773 / 6（50.10s）**（**+10 与新增用例数完全对上**，全在 `test_frontend_collection_guard.py`） |
| 覆盖率（阈值 84% 未动） | 86.72%、TOTAL 10,942 / 未覆 1,285 / 分支 2,692 / 部分 305 | 86.75% / 1,283 / 304 | **86.72% / 1,285 / 2,692 / 305**（与 C28 逐字相等，见 §9-3） |
| a2-check | 277 文件 | 281 文件 | **283 文件**（+6 = 守卫 + 4 个证据脚本 + 1 个测试文件），ruff/format/mypy/bandit 四项全 `ok` |
| public API | 466/466 双 100% | 473/473 | **473/473 双 100%**（+7 = 守卫的 6 个模块级函数 + `Verdict.ok`，用 `ast` 复核过数目） |
| 棘轮五项 | 274/2144/1044（快照 277/2145/1045，3 项 improved）、mypy 21、bandit 4 | 同 | **同，三条 improved 仍未 `--update` 冻结** |
| `frontend-collection` | **不存在** | `PASS: 13/13` | **`PASS: 13/13`**（本轮新增的 gate 检查项） |
| `frontend-test` | 8 files / 79 tests | 13 / 99 | **13 files / 99 tests（3.51s）** |
| eslint / vue-tsc | 0 errors / 48 warnings、rc=0 | 0 / 46、rc=0 | **0 errors / 46 warnings**（`§9-6` 已归因）、vue-tsc rc=0 |

## 11. 遗留（明确不做 / 做不了，含原因）

1. **e2e 面 3 文件 / 21 例（`auth` / `scripts` / `tasks`，playwright）仍不在 `make gate`**。
   它需要浏览器与起得来的后端，属另一个 runner；本轮只把它钉成白名单里**有名有姓**的边界，
   并在 census 里单独分面报告（21 例这一数字是"未被门禁覆盖"的规模，不是本轮成果）。
2. **`StatusBadge.test.ts` 仍带着与 StatCard 同一形状的死桩**（`global.components['el-tag'] = ElTagStub`，
   3 处 mount）。区别在于它的三条断言都落在真实文本上，所以**今天没有假判决**；
   但那个桩永远不会被用到，是同类缺陷的下一次生长点。本轮没有顺手删——它不影响判定，
   而本轮每一处 diff 都已各有归档可对。
3. **被静默的 5 个组件里 4 个零使用点**（`EmptyState`/`FilterBar`/`PageHeader`/`StatusBadge`，
   census 已排除生成物）。「这几个通用组件还要不要留」是产品决定不是测试决定，
   本轮只把它们接入运行面；现在它们真的在跑了，下一轮若要删，删的是**有读数的**面。
4. **前端覆盖率门今天不在 gate 里**：`make gate` 跑 `frontend-test`（不带 coverage），
   `thresholds`（lines 60 / functions 60 / branches 50 / statements 60）只有 `npm run test:coverage` 才评估
   ⇒ AC-17 的前端子项收口的只是「采集面全不全 + lint + 类型」，覆盖率那一半仍未接。
5. **守卫判的是文件级，不是用例级**：同一个文件里 `describe.skip` 一半它看不见。
   本轮那 18 例的规模来自 census 的静态调用点计数（与 vitest 报数逐字相等，§1），
   不是运行时读数；要做成用例级需要读 vitest 的 JSON 报告树，属下一轮。
6. **`frontend-test-cov` 与 `frontend-collection` 之间没有联动**。若将来把前端覆盖率接进 gate，
   应复用同一个 `judge()` 而不是再写一份规则（C27 那句「测量面可以独立，判定面必须唯一」在这里同样成立）。
