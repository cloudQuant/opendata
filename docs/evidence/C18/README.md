# C18 轮：修 ths 指数腿的裸码解析（AC-4 / AC-10 / AC-17 / AC-19）

> C16 轮登记的那条缺陷本轮做完。缺陷面不止「裸码解析不通」：**ths 的 `a-share-index` 目录自 C9 起整表取不出来**
> （1,431 行里有 206 行 `thscode` 去后缀 ≠ `ticker`，而 `normalize_instruments` 是**整页失败关闭**）。
> 本轮改的是一条对账规则的**作用域**，而「哪种不等算合法」这个决策**用测量定，不用猜**。全程数仓**只读**。

## 1. 缺陷到底是什么，以及它为什么藏了九轮

同一处整表拒造成两个症状，第二个比第一个严重，但第一个才可见：

1. **裸码路径全死**：`resolve_index_code` 第一步就是取指数目录（`_resolve_by_listing`），目录归一化必抛
   `ticker_code_mismatch` ⇒ 任何裸指数码都抛，**与这个码本身对不对无关**。
2. **指数目录本身不可用**：`instrument` 域按 `asset_type="a-share-index"` 取数走的是同一个归一化 ⇒
   C14 之后指数字典这条路一直是断的。没有一条判据覆盖它，所以九轮里没人看见。

为什么单测没测出来（这才是「绿的但假的」那一类）：唯一验过这条路的离线用例
`test_plain_index_code_resolves_from_the_index_universe` 的夹具**省略了 `ticker` 字段**，而
`_codes_agree` 对缺失 ticker 直接返回 `True` ⇒ 用例覆盖的是「上游不给 ticker」这个假象。
本轮把三条相关用例的载荷补成上游真实行形（带 `ticker` + `asset_type`）后，旧断言**立刻挂**，
新规则才是真的在解决问题。

## 2. 决策依据：先把账量清楚，再动规则

测量脚本 `index-code-rule-audit.py`（真机、只读、不落仓），四个 section：

### 2.1 各目录「`thscode` 去后缀 ≠ `ticker`」行数（不含既有豁免）

| 目录 | 行数 | 不等 | 处置 |
| --- | --- | --- | --- |
| a-share | 5,578 | **0** | 对账照旧逐字生效 |
| fund-etf | 1,696 | **0** | 同上 |
| a-share-index | 1,431 | **206** | 本轮缺陷面 |
| futures | 1,142 | 174 | 合成序列已豁免，剩余为真实不等（仍生效） |
| options | 10,000（超单页上限，只量第一页） | 404 | 不透明 `thscode` 已豁免 |

### 2.2 206 行的形态只有两类，且两类都合法

- **201 行：`ticker` 含非数字 —— 通达信自己的指数编号。里面就有主系列本身**：
  `000001.SH` ↔ `1A0001` 上证指数、`000002.SH` ↔ `1A0002` Ａ股指数、`000003.SH` ↔ `1A0003` Ｂ股指数、
  `000004.SH` ↔ `1B0001` 工业指数；「旧码」只是它的一个子集（`991001.TI` ↔ `1C0003` 上证指数(旧)）。
- **5 行：纯数字变体，名字带币种/份额标记**：`970006.SZ` ↔ `988006` 创业板指(港币)(CNH)、
  `470006.SZ` ↔ `988106` 创业板R(港币)(CNH)、`480001.SZ` ↔ `988201` 湾创100R。
- 201 + 5 = 206 ⇒ **0 行无法归类**，没有第三类在暗处。

⇒ 上一轮（C16 §7）写的「206 行 = 旧代码 + 港币双列」这个二分法**不成立**：占绝大多数的 201 行是
**在用另一套编号**，不是脏数据也不是过期码。

### 2.3 两条备选处置都被实测否掉

- **「只丢掉不等的行」是错的**：那样会把上证综指（`000001.SH`）自己从目录里丢掉，
  而 `991001.TI` 这类旧码在 K 线端点上仍然活着可取。
- **「用值规则区分合法与脏」不存在**：201 行含字母数字、5 行纯数字，两类都合法，
  任何取值规则都会把它们与真脏数据混在一起 —— 所以只能按**资产类型**划界。
- **「对账换不来任何保护」**：`ticker` **既不落库**（`Instrument` 没有该字段，normalize 后只剩 `thscode`）
  **也不参与裸码解析**（`_resolve_by_listing` 只匹配 `thscode` 前缀）。
  实测 **1,431 个不同前缀、0 个前缀有多行** ⇒ 裸码在指数目录里唯一
  （`000300 → 000300.SH`、`000001 → 000001.SH`、`991001 → 991001.TI`）。
  上一轮担心的「裸码在上游对多行」**经实测为 0**，那条注释本轮已按事实改掉。
- 唯一真正解析不出来的候选是 `932000`：**目录里根本没有这一行**（不属于该指数宇宙），与本轮规则无关。

### 2.4 落地的规则

`DISPLAY_CODE_ASSET_TYPES = frozenset({INDEX_ASSET_TYPE})` —— 与既有的
`OPAQUE_CODE_ASSET_TYPES = {"options"}` 同一族、同一机制，理由不同（期权是 `thscode` 不透明；
指数是展示码另成一套编号）。豁免只作用于**指数**：股票/ETF 实测 0 行不等，那些资产类型上对账继续逐字生效。

## 3. 改动面（4 个文件，175 insertions / 15 deletions）

| 改动 | 内容 | 落点 |
| --- | --- | --- |
| 规则作用域 | `_codes_agree` 增一条「`asset_type ∈ DISPLAY_CODE_ASSET_TYPES` ⇒ 不参与对账」；新增常量 + 注释里写进本轮实测数字；`__all__` 登记；`normalize_instruments` 的「代码对账」条目改写为三类豁免各写各的理由 | `opendata_fuyao/endpoints.py` |
| 探针改回裸码 | `("index_daily", "ths")` 的 `symbol` 由 `000300.SH` 改为裸码 `000300` ⇒ 每日巡检顺带覆盖「目录解析」这一层；`index_constituent` **保留带后缀**（穿透路由那条路也要有人验，且目录挂了不至于同时打死两条腿） | `opendata/pipeline/patrol.py` |
| 注释更正 | 上一轮那句「裸码 `000300` 在上游对多行指数」是**错的** ⇒ 改为如实说明这条探针为什么专门走解析路 | `opendata/pipeline/patrol.py` |
| 夹具求真 | 三条相关用例的载荷补成真实行形（带 `ticker` + `asset_type`），旧夹具的假绿面见 §1 | `tests/test_ths_provider.py` |

新增用例 4 条（3 离线 + 1 真机）：豁免成立（`1A0001`/`1C0003`/`988006`/`000300` 四种形态同页）、
**豁免不泄漏**（混合页里一条 `a-share` 不等仍整表抛）、展示码行不影响裸码查表（`000300` 与 `000001` 都解析得到）、
真机经注册表按裸码取指数日线（`symbol="000300"` ⇒ 5 行且 `Bar.symbol` 全为 `000300.SH`）。

## 4. 判据与留档（九份产物）

| 面 | 判据 | 留档 | 结果 |
| --- | --- | --- | --- |
| 测量面（决策依据） | 5 类目录逐类计数 + 206 行形态分类 + 前缀重数 + 裸码候选（可重放、只读） | `index-code-rule-audit.py` / `.txt` | 201 + 5 = 206（0 行未归类）、**0 个重复前缀**、`932000` 目录外 |
| 功能面（真机） | 5 个裸码解析 + 指数两条腿按裸码取数 + 目录页可读 | `index-bare-code-live.py` / `.txt` | 5/5 解析成功；`index_daily: 5 行 symbol=['000300.SH'] 2024-09-02..09-06`；`index_constituent: 300 行`；目录 **1,431 行**且 `000001.SH` / `991001.TI` / `970006.SZ` 三行都在（后缀分布 SH 198 / SZ 375 / TI 855 / CSI 2 / BJ 1） |
| 真值面（跨 vendor） | 裸码解析出的 `000300` 与 sina 官方指数序列逐字段对照 | `index-bare-code-cross-vendor.txt` | 4 腿 / **178 交易日**、重叠 1.000、OHLC 最大相对偏差 ≤1.33e-06、`result: PASS (0 failing legs)` |
| 巡检面 | 19 条全 `[ok]`，其中 `index_daily` 走裸码解析路 | `live-patrol.txt` | `ths/index_daily: 5 rows, 2028ms`、`失败 0 项；必配 Key 缺失 1 项`、`PATROL_EXIT=0` |
| 参数面回归 | C16 的离线审计重跑，与 C16 归档的 diff 必须**只**是本轮改动面 | `probe-param-audit.txt` | 19/19；`DIFF_LINES=2`（唯一差异即 `index_daily` 的 `000300.SH` → `000300`） |
| 规则真吃得住（变异） | 把 `DISPLAY_CODE_ASSET_TYPES` 清空 ⇒ 期望恰好新用例挂 | `mutation-no-index-exemption.txt` | `2 failed, 25 passed, 151 deselected`，两条失败分别是整表抛 `ticker_code_mismatch` 与裸码解析抛 ⇒ 载荷有效；文件按字节还原 |
| 单测面 | 3 条离线新用例（见 §3） | `tests/test_fuyao_endpoints.py`、`tests/test_ths_provider.py` | 全过；`opendata_fuyao/endpoints.py` **95.07%** |
| 真机 provider 类 | 11 条既有 + 本轮新增裸码用例一起跑（仅网络，不碰数仓） | `live-ths-legs.txt` | **11 passed / 1 failed**（失败项不是本轮改动，见 §5） |
| 质量门 | `make gate` 全绿（8 个 stage） | `gate.txt`（**5,763 行完整未裁剪**＝15 行跑前采集的运行环境头 + 5,748 行原始输出，`GATE_EXIT=0` 读自文件内部） | 见 §6 |

## 5. 一条失败的断言没有被放宽，另立 C19

真机 12 例里挂的那条是 **C14 的目录用例**：`assert sum(1 for row in rows if row.list_date is None) <= 20`
⇒ 本轮实测 **5,578 行全空**。

- 本轮**没有**调阈值、**没有** skip。阈值来自 C14 的实测（当时整表仅 9 行 null），现在换的是**上游事实**而不是判据。
- 字段级复算（`instrument-list-date-canary.py` / `.txt`，同一份快照 `2026-09-25 09:00:21 CST`）：
  a-share **0/5,578** 非空、fund-etf **0/1,696**、a-share-index **1,431/1,431**、futures 877/1,142
  ⇒ 塌陷**按资产类型分面**，不是整表清空（所以也不是「上游一次性不给了」）。
- 两种解释都还活着，本轮**只读采样无法区分**：①上游改了 a 股/ETF 的 `list_date` 契约；
  ②盘中目录构建时该列后填（快照时刻 09:00:21 早于开盘）。区分办法是收盘后（16:00）再采样一次，
  已登记 C19（task #25）。
- 比这条断言更重要的，是本轮暴露的**判据缺口**：巡检的 `instrument` 探针只数行数
  （实测 5,578 行 ⇒ `[ok]`），对字段级空值**完全无感**；而 C14 曾把「目录 `list_date` == sina 首根」
  当作该腿 verified 的依据之一 ⇒ **行数量级判据看不见字段塌陷**。C19 要评审的就是这条腿的
  `verified` 依据与巡检是否需要字段级 canary。

## 6. `make gate` 关键读数（完整输出在 `gate.txt`，`GATE_EXIT=0`）

| stage | 读数 |
| --- | --- |
| brand-check / zero-dep-check / js-points-check | PASS |
| a2-check | A2 文件 **247** 条（**+3** 恰为本轮三个证据脚本 `index-code-rule-audit.py` / `index-bare-code-live.py` / `instrument-list-date-canary.py`）：ruff / format / mypy / bandit 四项全 `ok` |
| quality-ratchet | 债务未增且三项继续改善：`ruff_selfdev 277→274`、`ruff_ported 2145→2144`、`direct_http_ported 1045→1044`；`mypy_selfdev 21` / `bandit_selfdev 4` 持平（**未 `--update` 冻结**） |
| public-api-quality | 公共可调用 **435** 条，docstring 100%、注解 100% |
| test-cov | **2,543 passed / 5 skipped in 46.41s**，分支覆盖率 **86.09%**（门槛 84%，`precision = 2`） |
| 本轮改动文件实测覆盖 | `opendata_fuyao/endpoints.py`：378 语句 / 15 未覆 / 170 分支 / 12 partial ⇒ **95.07%**（C15 为 95.03%）；`opendata/pipeline/patrol.py` **94.53%**（与 C16 持平，本轮只改注释与一条参数） |
| frontend-lint / typecheck / test | PASS：eslint 0 errors、`vue-tsc --noEmit` 通过、vitest **8 files / 79 tests** |

- 用例增量完全对上：2,540 → **2,543**（+3 恰为 §3 的三条离线新用例，真机那条按 `-m "not e2e"` 不入面）；
  `5 skipped` 与 C12–C16 逐字相同（仍全部是 AC-6 那批 em 夹具，本轮不拿它当绿灯证据）。

## 7. 边界：本轮**没有**做什么

- **只对指数豁免**。股票 / ETF / 期货 / 期权的对账规则一字未动，且有专门用例钉住「豁免不泄漏」
  （混合页里一条 `a-share` 不等仍整表抛）。
- **没有按值放宽**：不引入「含字母就放过」这类规则 —— §2.3 已证明任何取值规则都会把合法展示码与脏数据混起来。
- **不改 `resolve_index_code` 的匹配策略**：仍只按 `thscode` 前缀匹配且要求唯一命中。
  裸码唯一性是**实测**换来的（1,431 前缀 0 重复），因此不新增「用 `ticker` 兜底」的第二路；
  上游哪天出现重复前缀，现有 fail-closed 语义会抛而不是猜。
- **不动落库、不建表**：数仓全程只读；`dwd_instrument` 仍未落地（生产写入 + 建表需明确确认）。
- **C19 的字段级判据本轮不实装**：判据设计要先分清「契约变更」还是「时序」，两者处置相反，
  现在写死任何一条都会把猜测固化成判据。
- **前轮证据不被后续轮次改写**：`scripts/ops/ths_index_cross_check.py` 会把报告写进
  `docs/evidence/C5/ths-index-cross-check.txt`，本轮跑完该文件被改动（只有窗口行）⇒ 已 `git restore` 还原，
  本轮的对照输出另存为 `index-bare-code-cross-vendor.txt`。
- `tests/fixtures/upstream/stock_daily_{raw,qfq}/meta.json` 由录制流程每次跑测试时重写，
  本轮**不纳入提交**（AC-6 的阻塞面与本轮无关，仍为 `push2delay.eastmoney.com` 不可解析）。
