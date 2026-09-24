# C9 · ths 指数成分股腿：接上端点、量清上游语义、跨 vendor 逐成员转正（AC-10）

对应验收项：**AC-10（1C/C1 provider 扩充）**、AC-5（P1/P0 域数据源与 fidelity）、
AC-7（端点映射表条目状态）、AC-17（覆盖率与门禁复跑）。

`index_constituent` 在 C1 轮只有 akshare 一条腿（中证官网的**月末权重文件**），ths 侧
在映射表里挂着 `available`、在 `openbb_map.yaml` 里是 `pending`。本轮把该端点接进
`opendata_fuyao` + ths provider，并用**指数公司自己的当日清单**做跨 vendor 对照，
逐成员比对后才把 `verified` 翻成 `true`。

一条成分股清单和一条日线不一样：**没有「偏差」可言，只有「这个成员在/不在」**。
所以本轮判据不是阈值，而是集合恒等 —— 这也意味着参照面必须选对（§4 记录了一次
选错参照的实测）。

## 1. 产物

| 文件 | 作用 |
|---|---|
| `opendata_fuyao/endpoints.py` | `INDEX_CONSTITUENTS_ENDPOINT` + `build_index_constituents_request`（单指数、必须带后缀）+ `normalize_index_constituents`（信封→契约，含两路成员标识对账）+ `fetch_index_constituents` |
| `opendata/data/providers/ths/models/index_constituent.py` | `ThsIndexConstituentFetcher`：`Capability(period="snapshot", verified=True)`、日期区间请求 fail-closed、成员码双侧去后缀 |
| `opendata/data/providers/ths/registration.py` | `FETCHERS` 5 → **6** 条腿 |
| `opendata_fuyao/endpoint_map.py` / `.yaml` | 该端点 `available → implemented`（常量清单 + 状态行同步） |
| `opendata/data/openbb_map.yaml` | ths `index_constituent` `status: pending → verified` |
| `scripts/ops/ths_index_constituent_cross_check.py` | 真机跨 vendor 对照（7 腿：5 标准 + 1 双写法 + 1 只观测板块），自带节流与退避 |
| `ths-index-constituent-cross-check.txt` | 转正判据：6 条判定腿 PASS，`KNOWN_DEVIATIONS` 为空 |
| `gate.txt` | 完整未裁剪门禁日志（5,465 行） |
| `tests/test_fuyao_endpoints.py` | +7 项（请求构造 1 / 归一化 4 / 取数 2） |
| `tests/test_ths_provider.py` | +4 项（含裸码解析、日期区间拒绝、空响应失败关闭）+ 注册与权威序断言同步 |

## 2. 上游语义实测清单（先量，再写代码）

| 事项 | 实测 | 落点 |
|---|---|---|
| 入参形态 | 只接受**单个** `thscode`：逗号分隔的批量写法、裸码、不存在的码、`.CSI` 后缀均返回 `code=1002`「请求参数超出取值域」；空值/缺参返回 `1001` | `build_index_constituents_request` 本地先拦裸码（`constituents_symbol_qualified`），让调用方走 `resolve_index_code` 而不是在这里猜后缀 |
| 分页 | `limit` / `offset` 被忽略，**整份清单一次给全**（实测 300 / 50 / 500 / 50 / 1000 / 204 行各一次请求） | `fetch_index_constituents` 单次请求；对照脚本报头显式声明该事实 |
| 行结构 | 每行只有 `{thscode, ticker, name}` —— **没有权重**，也没有生效日 | `weight=None` 如实落契约，不从参照侧借数、不本地补算 |
| `data.timestamp` | 是**请求时刻**的毫秒戳（精确到毫秒、每次调用都变），不是数据生效时间 | `as_of = 请求时刻的上海日期`，语义为「观测日」，写进模块与归一化器 docstring |
| 成员标识 | `thscode` 去后缀与 `ticker` 在本轮 2,404 行上全部一致；无重复行 | 两路**对账**，不一致即 `constituent_symbol_mismatch`（落库用裸码做合并键，猜错一路不会报错，只会把成分股接到另一个标的上） |
| 突发限制 | 连续调用直接被断连（`FuyaoError: network / ConnectError`），约 10–20 秒后恢复 | 脚本自带 `PACE_SECONDS=12` 节流 + 5×20 秒退避；**跑不通**记为该腿 FAIL，不混进**数据不对** |
| 同一指数的多种写法 | `000300.SH` 与 `399300.SZ` 返回**同一份**成员集合 | 作为一条判定腿（`DUPLICATE_LEGS`）：既与发布方比，也与自己的另一写法比 |
| 板块指数 | `886042.TI` 可取（204 行），但同花顺自定板块**没有独立发布方** | 列为只观测腿（`OBSERVE`）：形状/权重/索引码判据照打，集合判据不适用 |
| 北交所成员 | 标准指数清单不含 BJ 标的，只在 THS 板块里出现 | 记录为口径事实，不在本轮改动（对照两侧同口径） |

## 3. 存什么码：双侧都用裸 6 位码

bar 域的 ths 腿把**带后缀的** thscode 存进 `symbol`（那是行情标的的唯一身份）。
成分股腿相反，`index_symbol` 与 `symbol` 都存裸码：

* 成员身份是**跨源合并键**。akshare 那条腿（`index_constituent`）同样给裸码，若
  ths 侧存 `600519.SH`，双源落库会写成两行、而不是同一事实的两个观测；
* 请求用的后缀只在请求里体现：`000300.SH` 与 `399300.SZ` 归一化后是同一份清单，
  本轮判定腿正是用这一点做自证（jaccard 1.0000、重复行 0）。

该选择写进模块 docstring，并由对照脚本作为**硬判据**（`bad_shape`：非 6 位纯数字即
FAIL；`index_symbol != 请求裸码` 即 FAIL），而不是只靠注释。

## 4. 参照面选型：一次选错的实测

| 候选 | 实测 | 结论 |
|---|---|---|
| akshare `index_stock_cons_weight_csindex`（权重文件） | 文件内 `日期` 是**上月末**（本轮为 2026-08-31）。与当日清单比：沪深300 300/300、上证50 50/50、中证500 500/500 全等，**科创50 差 5/50（jaccard 0.8182）** | 弃用。差异是**版本日不同**而非数据缺陷，拿它当判据会把「谁在 9 月被调进调出」记成 ths 的错 |
| akshare `index_stock_cons_csindex`（官网当日清单） | `日期` = 运行当日（2026-09-24），五条腿 jaccard 全部 1.0000 | 采用。它既是**另一个 vendor**，又是**决定谁进指数的那一方** |

选参照的标准不是「有没有现成函数」，而是「能不能区分口径差与版本差」。第一条
候选在 4/5 条腿上给满分、在 1/5 条腿上给 0.8182 —— 这种**只错一条腿**的形态最危险，
因为它看起来像上游数据质量问题。

## 5. 真机跨 vendor 对照（转正判据）

```
python scripts/ops/ths_index_constituent_cross_check.py   # EXIT=0
```

对照面：`get_registry().resolve_domain('index_constituent', source='ths').fetch(...)`
（调用方真实路由路径，即适配层归一化后的输出）↔ 中证指数官网当日成分股清单。

| leg | thscode | 参照行数 | as_of 我们/发布方 | 成员数 | jaccard | ths 多出 | 参照多出 | 重复行 | weight 全空 | 结论 |
|---|---|---|---|---|---|---|---|---|---|---|
| 沪深300 | `000300.SH` | 300 | 2026-09-24 / 2026-09-24 | 300 | 1.0000 | - | - | 0 | 是 | PASS |
| 上证50 | `000016.SH` | 50 | 2026-09-24 / 2026-09-24 | 50 | 1.0000 | - | - | 0 | 是 | PASS |
| 中证500 | `000905.SH` | 500 | 2026-09-24 / 2026-09-24 | 500 | 1.0000 | - | - | 0 | 是 | PASS |
| 科创50 | `000688.SH` | 50 | 2026-09-24 / 2026-09-24 | 50 | 1.0000 | - | - | 0 | 是 | PASS |
| 中证1000 | `000852.SH` | 1000 | 2026-09-24 / 2026-09-24 | 1000 | 1.0000 | - | - | 0 | 是 | PASS |
| 沪深300 另一种写法 | `399300.SZ` | 300 | 2026-09-24 / 2026-09-24 | 300 | 1.0000 | - | - | 0 | 是 | PASS |
| 同花顺板块 | `886042.TI` | 无独立发布方 | 2026-09-24 | 204 | - | - | - | 0 | 是 | OBSERVE |

6 条判定腿 = **2,200 个「指数 × 成员」观测点**，集合完全相等（多出/缺少均为 0），
两侧观测日同为运行当日（lag 0 天）；板块腿另计 204 行为只观测。合计 2,404 行。
本轮 `KNOWN_DEVIATIONS` 为**空**：没有任何一条成员差异需要登记，因此也不存在会过期
的例外。腿的选点覆盖 50/300/500/1000 四种规模，使「少了一个成员」不可能同时骗过所有腿。

### 判据为什么是集合恒等而不是阈值

| 判据 | 界 | 依据 |
|---|---|---|
| 成员集合 | jaccard **恰好 1.0000**，多/缺都列出成员码 | 成分关系是离散事实，两家发布方若有分歧只可能是「谁进出指数」，不存在 1e-6 级别的可信残差 |
| 观测日 | 与发布方清单日相差 ≤ 1 天 | 两侧都是「当日观测」，允许 1 天只为跨过运行时刻落在上海零点两侧的情况；实测 lag 0 |
| 行形状 | 成员码必须是 6 位纯数字、`index_symbol` 必须等于请求裸码、无重复行 | 不满足即为**口径**出错（后缀污染合并键 / 归一化选错字段），不是数据差异，必须与集合判据分开 |
| 权重 | 每行 `weight is None` | 上游不发布权重；若某天真出现非 None，说明有人在适配层里补算或借数，那是要立刻暴露的事 |
| 例外登记 | 双向过期 | 未登记的差异 ⇒ FAIL；已登记的差异回到一致 ⇒ 同样 FAIL（与 C8 同一纪律） |

## 6. 注册事实变化（复算口径）

| 口径 | 本轮前 | 本轮后 |
|---|---|---|
| 注册表能力 / 数据域 | 27 / 15 | **28 / 15**（新腿落在已有域 `index_constituent`） |
| 其中 `verified` | 14 | **15** |
| ths fetchers | 5 | **6** |
| `endpoint_map` `implemented` | 11 | **12** |
| `endpoint_map` `available` | 80 | **79** |
| `openbb_map.yaml` ths `index_constituent` | pending | **verified** |
| 落库权威序 `authority.json` | `index_constituent: [ths, akshare]` | **不变**（见下） |

`authority.json` 本轮**不动**，但要说明它此前的一处尴尬：这张表一直把 ths 排在
`index_constituent` 的权威首位，而 ths 那条腿在 C1 之后仍是未验证状态——排序先于
证据存在。本轮的实测正是把该排序坐实（`tests/test_ths_provider.py` 新增断言
`authority_baseline()["index_constituent"][0] == "ths"`，把「首位是谁」钉成断言而不是
只留在 JSON 里）。表本身无需改：双源合并的权威序口径（dwd 层 `ths > akshare`）不变。

## 7. 离线单测与本层覆盖率

新增 11 项测试函数，全部**无网络**（fake httpx client + 合成信封）。按阶段分组：
请求构造 1（单指数、必须带后缀、只 trim 不改大小写）、归一化 4（裸码双侧 + 按成员码
升序、两路标识对账、缺 `data.timestamp` 失败关闭、空 `index_symbol` 失败关闭）、
取数 2（整份清单单次请求 + 参数逐字断言；未注册指数 `code=1002` → category `request`）、
适配层 4（双侧裸码、裸码经 `resolve_index_code` 解析、日期区间 →
`THS_CONSTITUENTS_SNAPSHOT_ONLY`、空响应 → `THS_EMPTY_RESPONSE`）。

新腿同时自动获得通用契约用例：`tests/test_provider_conformance.py` 按 fetcher 参数化
（类内 4 个方法），108 → **112**，即注册即被契约一致性检查覆盖。

门禁报告里的本层实测（`gate.txt` 内 `--cov` 表原行）：

| 文件 | 覆盖 | 未覆盖处 |
|---|---|---|
| `providers/ths/models/index_constituent.py` | **100.00%**（25 语句 / 4 分支，0 partial） | — |
| `opendata_fuyao/endpoint_map.py` | **100.00%**（140 语句 / 48 分支） | — |
| `opendata_fuyao/endpoints.py` | 92.65%（225 语句 / 88 分支 / 9 partial） | 未覆盖行全部属**其他**端点分支（42-43, 140, 297, 299, 347, 397, 440, 454-455, 479, 501, 504-505），本轮新增的三个函数无缺口；≥90% 为 C7 定的该包口径 |

## 8. 门禁复跑

```
make gate      # EXIT=0，完整未裁剪日志 gate.txt（同目录，5,465 行）
```

| 项 | 本轮实测 |
|---|---|
| brand / zero-dep / js-points | OK（品牌 token 零残留；上游引用基线 3 未增；引擎 76 / eval 35 / exec 0） |
| a2-check | **228 文件**（C8 为 226，+2 = 新适配层 + 新脚本）：ruff / format / mypy / bandit 全 ok |
| quality-ratchet | 五项**等于快照**（ruff_selfdev 277 / mypy_selfdev 21 / bandit_selfdev 4 / ruff_ported 2145 / direct_http_ported 1045，未增债） |
| public-api-quality | A2 公共可调用 **403**（C8 为 397，+6），docstring 与注解双 **100%** |
| test-cov | **2,414 passed / 5 skipped**（C8 为 2,399：+11 新测试函数、+4 为 conformance 参数化自动展开）、覆盖率 **85.53%**（C8 85.44%，阈值 84%） |
| 前端 | eslint 0 errors / 48 warnings（存量待清）、`vue-tsc` 通过、vitest **8 files / 79 tests** |
| 运行环境 | `branch dev`、`HEAD ae4acfd`（本轮 gate 前后无新提交，故 HEAD 可事后确认）；`gate.txt` 第 293 行的 pytest 抬头带 `platform darwin -- Python 3.13.5, pytest-8.4.1` |

与 C8 的一处差异如实记在此：C8 的 `gate.txt` 开头有**跑前采集**的环境抬头（含脏文件清单快照），
本轮的日志是直接 `make gate 2>&1 | tee` 落盘、**没有**那段抬头，事后也无法伪造「跑当时的脏文件
集合」。因此本轮只声明可从 git 复算的 branch/HEAD，不补写脏文件清单。

## 9. 边界与待办

* **快照口径，不是历史口径**：该端点只给「当日成员」，`as_of` 是观测日。做指数增强
  回测需要「当时的成分」，本轮**不**声称覆盖——日期区间请求被显式拒绝
  （`THS_CONSTITUENTS_SNAPSHOT_ONLY`），就是为了防止有人把今日清单当历史用。点位
  化需要按日归档或另一条 PIT 数据源，属后续迭代。
* **无权重**：`weight=None`。C1 轮 akshare 那条月末权重腿仍在册，两腿互补而非替代；
  合并时以「裸码 + 观测日」为键，月末权重文件与当日成员清单的**版本日不同**（§4）
  必须在落库侧显式区分。
* 板块腿（`886042.TI`）只观测不判定：同花顺自定板块没有独立发布方，判不了对错。
* 上游突发限制（§2）意味着**批量取数不能并发**：整表刷新一次要 1.4k 指数 × 12 秒，
  实际调度必须先按 `catalog/ths-index-list`（映射表里仍为 `available`）选子集。
* `docs/迭代计划/…/验收文档.md` 的 AC-5 / AC-10 / AC-17 与 v3.1 版本行按本轮事实回填。
