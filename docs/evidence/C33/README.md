# C33 轮：把"批次进度以清单为准"变成一句判据（provider 清单 ↔ 注册表全库对账，task #41）

> 版本：本轮收口 v5.4 验收回填（AC-1 / AC-10 / AC-17）
> 日期：2026-09-26
> 前置：C23 把 `authority.json` 与注册表对账（"复制的那一份就是下一个会漂移的地方"）；C31 让
> `frontend-typecheck` 真读树；C32 把 e2e 面的 7 条缺口退空后换了条能红的规则。本轮是同一族
> 的第 4 个实例，只是对象换成**规划文档自己**：`docs/proposals/openbb-migration/README.md` §5
> 写着"批次进度以本文件与 `provider-inventory.yaml` 的 `status` 字段为准"，而本轮之前**树上没有任何
> 一处代码读过那个文件**——一份只写不读的清单，量出来是下面 §1 那样。

---

## 0. 一页结论

| 项 | 读数 |
| --- | --- |
| `make gate` | `GATE_EXIT=0`，`docs/evidence/C33/gate.txt` **12,941 行完整未裁剪（含两次跑）**，读数取自文件内部；第 2 次跑（11:03 采集，头部写在启动前）对应提交树：七条规则 + 守卫 28 例 |
| 判定面 | `scripts/quality/openbb_inventory_plane.py`：七条规则 `PARSE`／`FIELD SHAPE`／`MISSING ROW`／`DUPLICATE ROW`／`STALE STATUS`／`COUNT CLAIM`／`RIGHTS LINK`；`MODEL ELISION` 只报告不判定 |
| 修之前 | **14 条 finding**（`PLANE_EXIT=1`）：清单不可解析、32 行全写 `待实现`、`akshare`(10)+`ths`(11) 两条最大源**根本没有行** ⇒ 33 条能力里 21 条对这个"进度准绳"不可见；头部自报 33 个目录而正文 32 |
| 修之后 | **0 条 finding**（`PLANE_EXIT=0`）：`record rows 34 = providers 32 + local 2`，`record statuses ['已实现未对照','已对照转正','待实现']`，注册表侧 7 源 / 33 能力 / 20 条参与 auto，权利 §1 行 11 条 |
| 判据未放宽 | 未把任何一条期望值往实现方向挪：`STALE STATUS` 是**与注册表推出的状态逐字相等**，因此天生双向；`MODEL ELISION`（15 行截断名单）**没有**升格成判据——理由见 §4 |
| 守卫用例 | `tests/test_openbb_inventory_guard.py` **28 例**跑在 `test-cov` 里（本轮 23 例 → 28 例：新增 FIELD SHAPE 5 例），test-cov 总数 2,821 → **2,849 passed / 6 skipped** |
| 反证 | `falsification.txt`：**8 条植入全红 + 控制组全绿**（`DRIVER_EXIT=0`，survivors `(none)`），驱动器跑完即删 |
| 本轮不动的东西 | 没有实现任何新 provider 的取数逻辑；`opendata/` 与数仓 diff 为空；AC-15 旧表仍只读、未执行 DROP；全程未读取或打印任何密钥值 |

---

## 1. 先量，再改（四份测量，都写在树上而不是结论里）

清单文件被 README §5 指定为准绳，但没有任何代码读过它。本轮第一手读数：

| 量什么 | 读数（`provider-inventory.before.yaml` ＝ `git show HEAD:` 那份，`cmp` 验过逐字节相同） |
| --- | --- |
| 能不能解析 | **不能**：`sequence entries are not allowed here`（`credentials: -` 第 15 行第 18 列）——裸 `-` 是 YAML 的序列标记不是值，全文 **34 处**，所以任何判据都读不回它 |
| 名单字段的形状 | 64 处依赖字段（`sdk_dependencies` 32 + `credentials` 32）**没有一处是列表**：34 处裸 dash、**21 处裸标量**（`sdk_dependencies: xmltodict`）、**9 处逗号串**（`openbb-platform-api, openbb-economy, …`）。后两种**能**解析，只是解析成一个字符串 ⇒ `len()` 数出 1 个依赖、`"async-lru" in value` 是子串测试。`models` 32 行倒是 flow 列表 |
| status 对不对 | 32 行**全部**写 `待实现`；注册表侧 7 个源服务 33 条能力（20 条参与 auto）：ecb 3/3、imf 3/3、oecd 2/2、yfinance 1/1、fred 3/0、ths 11/11、akshare 10/0 |
| 覆盖不覆盖得到 | `akshare`（10 条）与 `ths`（11 条）**在两节里都没有行** ⇒ 33 条能力里的 21 条对这个准绳不可见；`ths` 还是 §7 真机场景的主源 |
| 自报数字 | 头部注释 `规模：33 个 provider 目录`，正文 32 行；`348 个 fetcher 模型` 同样没人钉（正文 `fetchers` 相加确实是 348，是巧合不是判据） |

判定面的模块 docstring 把上面这组读数原文写在了代码头部——一份只留档的结论会在下一轮被读成传说。

## 2. 判定面：一份模块，两个入口（C27 口径）

`scripts/quality/openbb_inventory_plane.py` 是唯一判定处；它同时是

* 门禁里的判据：`tests/test_openbb_inventory_guard.py` 28 例 import 它，跑在 `test-cov`；
* 手跑的档案生成器：`--inventory/--rights` 指哪份清单判哪份（before 档判的就是 HEAD 那份副本）。

树侧读数走**进程内注册表**（`register_providers()` + `get_registry().capabilities()`），因此
"这个源注册了几条、几条参与 auto" 不是抄来的清单而是当场问的；不联网、不连 MySQL、不读凭证。
注册表导入不了时 `exit 2` 而不是当成"树里什么都没有"（C28 口径：读不出来 ≠ 通过）。

清单侧刻意**不**依赖能否解析：`parse_rows()` 按行结构读，`parse_error()` 单独问真解析器 ⇒
`PARSE` 变红时其余规则仍在描述正文（这条在守卫用例 `test_bare_dash_scalars_…` 里钉住）。

## 3. 七条规则与本轮的三条设计决定

1. **`STALE STATUS` 判的是"与注册表推出的状态逐字相等"，不是"别倒退"。**
   `expected_status()`：0 条腿 ⇒ `待实现`；全部腿参与 auto ⇒ `已对照转正`；否则 ⇒ `已实现未对照`。
   三态里那个中间态是本轮才**有名**的：fred（3 条腿，0 条 auto）与 akshare（10 条腿，0 条 auto）
   此前只能被写成 `待实现`（假）或 `已对照转正`（更假）。因为判据是相等，两个方向都红：
   清单说得多（登记为零却写转正）与说得少（树上有 11 条腿却写待实现）是同一条规则的两个用例。
2. **两节而不是一节。** `providers:` 是"内化 OpenBB 上游的项"，`local_providers:` 是"我们自己在跑、
   上游没有的源"（ths、akshare）。akshare 不该混进上游清单，但它是国内域的 HTTP 退路、有 10 条注册
   能力——把它藏起来就是让准绳看不见系统里第二大源。`DUPLICATE ROW` 管的就是"两节都写"。
3. **不往 YAML 里抄可推导的东西**（C23 的直接教训）。清单只写**声明**（批次、场景、上游 fetcher 数、
   SDK 依赖、权利行名、状态），不写"我们注册了哪些域"的副本——那份的唯一事实是注册表
   （`GET /api/v1/sources`）。`rights_rows` 写的是权利登记表的**行名原文**，因为 AC-1 门禁 R3
   说未登记的源不得进 auto，跟踪路由进度的文件必须能指回登记行；行名对不对由 `RIGHTS LINK` 判。
4. **`FIELD SHAPE` 只在 `PARSE` 绿时判。** 一条 `credentials: -` 同时会命中两条规则，把同一个坏行
   计两次会把 14 条读数inflate 成 30+；不可解析的文件也没有"字段类型"可判。这条取舍写进了 docstring，
   并有一条守卫用例（`test_the_shape_rule_stays_quiet_while_parse_reddens`）钉住。

## 4. 只报告不判定的那一类，以及为什么

`MODEL ELISION`（15 行把上游模型名单截断成 `[…, ...]`）**报告但不判**：一份说了"我截断了"的名单是
诚实的，而把它升格成判据只能靠**编出没写下来的名字**来满足——那正是本轮要防的漂移。截断标记
`...` 在 `flow_list()` 里被丢弃，因此它既不算一个模型名，也不算一条 finding（守卫用例
`test_truncated_model_lists_are_never_a_finding` 断言截断与完整名单在判据上逐字等价）。

同理，本轮**没有**把"上游 348 个 fetcher 是否都已内化"写成判据：`status` 只描述我们注册了的那部分
（yfinance 行 `fetchers: 29` 而本仓只注册 `stock_daily_overseas` 一条 ⇒ `已对照转正 ≠ 29/29 完成`），
这条读法约束写进了清单头部注释。

## 5. 证明它能红

| 面 | 跑法 | 结果 |
| --- | --- | --- |
| 反证（真树） | `falsification.txt`：8 条植入，每条只改清单一个字段，然后跑**门禁里那份**判据（28 例） | **全红**，控制组全绿，`DRIVER_EXIT=0`。F1 说少／F2 说多（凭空加一行转正）／F3 装回裸 dash／**F4 名单写成裸标量**／**F5 名单写了键没写值 ⇒ null**／F6 计数漂移／F7 删掉整条 ths 行／F8 权利行名错一个字。档案里逐条留着被判据点名的原文（如 `FIELD SHAPE: 'ecb'（providers）把 'sdk_dependencies' 写成 'xmltodict'，反序列化得到 str 而不是 list`） |
| 反证的限度（不粉饰） | 八条植入都由 `test_shipped_record_agrees_with_the_tree` 这条"整树对账"断言先接住，`test_report_exit_code_differs_between_the_two_records` 作第二见证 | 因此这轮**不声称**每条规则被单独隔离打红——单条规则的咬合力由 28 例合成用例负责（其中 5 例是 FIELD SHAPE 的正反两面）；植入档证明的是"改一个字段，门禁会红"，且每条 finding 文本点名了正确的那条规则与那个源 |
| 判定面自己的 bug | 修完 YAML 后第一次跑，平面报 `34 = providers 31 + local 3`、fetchers 345 | 根因是 `parse_rows()` 在**收尾**行时才记节名：最后一行 `wsj` 被下一节的节名接管。修法是在行头匹配时就钉住 `row_section`（代码注释留了原因），并加回归用例 `test_section_tagging_survives_a_second_section`——**这条是判定面量出来的，不是清单量出来的** |
| 权利面作用域 | `test_rights_table_scoping_ignores_the_todo_table` | 权利表 §2「待办与责任」也是编号表：首版按全文件读出 16 个"源"，把 §2 的句子当成数据源就能拿待办句子满足 RIGHTS LINK ⇒ 解析限定在 `## 1` 节，读出 11 行 |

## 6. 门禁读数（逐项，取自 `gate.txt` 第 2 次跑内部）

| 门禁项 | 读数 | 与 C32 比 |
| --- | --- | --- |
| test-cov | **2,849 passed / 6 skipped**，覆盖率 **86.75%**（TOTAL 10,942 stmts / 1,283 miss / 2,692 branch / 304 partial） | +28 例（恰为本轮守卫用例）；**不声称覆盖率改善**——判据住在 `tests/` 与 `scripts/quality/`，两者都在 `--cov` 分母之外（`pyproject.toml` 的 `source = [opendata, opendata_fuyao]`）；6 skip 与 C27–C32 逐字同批（`tests/test_port_fidelity.py` em 通道，em 仍拒） |
| a2-check | **A2 files: 295**（+2 = 判定面模块 + 守卫用例），`ok ruff check`／`ok ruff format --check`／`ok mypy`／`ok bandit` | C32 是 293；本轮两个新文件一开始就按 A2 标准写，无"先红后改"过程 |
| public-api | **502/502**，docstring 与 annotation 各 **100.0%** | +1（`field_shapes()`）；C32 为 487 |
| quality-ratchet | `ruff_selfdev` 274（快照 277）／`mypy_selfdev` 21／`bandit_selfdev` 4／`ruff_ported` 2144（2145）／`direct_http_ported` 1044（1045），三条 improved 未 `--update` | 与 C32 逐字相同（只降不升；本轮没动 `opendata/`） |
| brand-check / zero-dep / js-points | `no brand residue`；扫描器自测 `8 violations detected, 4 compliant samples clean`；js-points OK | 持平——清单里新增的是自研源与上游 SDK **名称**，无 openbb 发行包依赖、无源码复制（§2 洁净室口径不变） |
| frontend-lint | `✖ 33 problems (0 errors, 33 warnings)` | 持平（本轮没动前端） |
| frontend-typecheck | `npx vue-tsc -b --force` 无输出、rc=0；产物 `frontend/tsconfig.{app,e2e,node}.tsbuildinfo` 由 `frontend/.gitignore:16` 的 `*.tsbuildinfo` 兜住，跑后 `git status --porcelain` 无残留。**同步更正**：C32 §6 那行写的"跑后无 `*.tsbuildinfo` 残留"不准确——`-b` 一定会写构建态，正确说法是"写了但被 gitignore 兜住"（C31 §6 第 148 行的表述才是对的） | 持平 |
| frontend-collection / frontend-test | 14 文件 / **106 例全绿** | 持平（本轮没有新增前端用例） |
| frontend-e2e | 判定面 `findings: 0` / `named gaps: 0` / `static vs runner: MATCH` ⇒ 真跑 **`18 passed (7.0s)`** | 持平 |

两次跑的说明：第 1 次（10:49，`GATE_EXIT=0`，2,844 passed）跑完之后才补 `FIELD SHAPE`，树变了，
那次日志只作过程留痕，未裁剪、留在同一档案前半段；第 2 次（11:03 采集）对应提交树。

## 7. 登记不改的债与本轮边界

* **没有实现任何新 provider。** 本轮只把"进度准绳"修成可判定；P1/P2 的取数实现仍按 README §5 的
  175~195 人日排期，R2（商业源 Key）未变：fmp/intrinio/tiingo/alpha_vantage 等实现完但没有 Key
  就补不了真机对照，也就进不了 auto。
* **akshare 的 10 条腿仍不能进 auto。** `已实现未对照` 是它今天的真实状态；转正需要 AC-6 口径的真机
  逐字段对照，**不是一行 status 改动**（这条写在 akshare 行的 `notes` 里，本轮拒绝用改判据的方式让它变绿）。
* **`fetchers` / `models` 仍是上游事实的快照**，绑定在上游 commit `3e071fcc2c`。上游换 commit 时这两个
  字段会漂，本轮没有（也无法）在上树里校验上游包；`MODEL ELISION` 的报告行是给下一个读它的人的提示。
* **`FIELD SHAPE` 不检查名单内容**：它只保证解析出来是 list。`credentials: [随便写的键名]` 仍要靠
  运行时与 §6 的凭证约定；把凭证键名也纳入判据需要一份权威 env 名单，属接口决策。
* **清单仍是"声明"而不是"结果"**：它说 `已对照转正`，指的是注册表里那些腿的状态；对照证据在
  `docs/evidence/C*/` 各轮里，准绳与证据之间没有自动指针（除 RIGHTS LINK 这一条外）。
* **`docs/data-rights-registry.md` 升 v1.1**：§1 的 11 行登记与复核结论一字未动（仍是"待复核"），
  只在 §3 处置规则加了一条"可追溯性有判据"，AC-1 的"数据权利登记"这一项今天才算能被机器检查。
* 本轮动过的文件全在 `docs/` 与 `scripts/quality/`、`tests/`：`opendata/`、`opendata_fuyao/`、前端与
  数仓在 diff 里为空；AC-15 旧表仍**只读**，未执行任何 DROP；未读取或打印任何密钥值。

## 8. 档案清单

| 档 | 作用 |
| --- | --- |
| `gate.txt` | 两次完整门禁日志（12,941 行，未裁剪；两段各自头部写在启动前，段末各有 `GATE_EXIT`） |
| `inventory-reconciliation-before.txt` | 七条规则判 HEAD 那份清单的完整输出：`PLANE_EXIT=1`、14 条 finding、六类计数器 + `MODEL ELISION` 15 行 |
| `inventory-reconciliation-after.txt` | 同一份判据修后重跑：`PLANE_EXIT=0`、`record rows 34 = providers 32 + local 2`、`== findings (judged) == (none)` |
| `provider-inventory.before.yaml` | 修改前的清单副本（与 `git show HEAD:` 逐字节相同，`cmp` 验过）——守卫用例 `test_pre_fix_record_reddens_the_same_rules` 读的就是它，档案因此是**载荷**不是插图 |
| `falsification.txt` | 8 条植入 + 控制组的完整 pytest 尾部输出（每条 14 行，未裁剪）与撤销后的 `git status` |
