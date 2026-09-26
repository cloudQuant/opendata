# C28 轮：把 AC-19 的「配额/到期/封禁」演进项做成能落地的分级凭证面（task #36）

日期：2026-09-26｜分支：`dev`｜前置：C27（`083a678` 之后的 `aec3088` 判据收口）
本轮的入口任务是「把 AC-19 那句挂了五轮的演进项做掉」。它的原文是
**权威源 Key 配额/到期/封禁监控告警生效**，而 C16→C25 四轮一直在补它的**相邻面**（行数量级、字段级 canary、
抖动/故障可区分），这句本身每次都在 §10 结尾被重新登记为「确实未做」。
本轮做完之后，**它仍然只完成三分之二**：封禁（凭证被拒）与配额（429）现在可分类、可归责、可路由；
**到期没有做，也没有被写成做完了**——因为没有任何一个源公布 Key 的到期日（§5）。
下面这些读数是本轮真正的交付：**一条失败文本被拆成 9 类 5 档、每类署名一个处置方（9 个）**，
以及一条泄漏检查被证明**能红**（它先是恒真的，差一点就这样留档）。

---

## 1. 缺口从哪来：巡检早就"看见"了凭证失败，只是说不清是谁的事

`patrol()` 的 catch site 一直把异常压成一行 `f"{type(exc).__name__}: {exc}"` 存进 `PatrolResult.error`。
所以 401、Key 没配、配额耗尽、源挂了这四件事在报告里**长得一模一样**：一行文本、一个非零退出码、
一个把整个源从 `auto` 路由摘掉的健康位。可操作读数只有「ths 有点问题」。

更糟的是这一行文本本身不带判定信息。数一下**本轮之前所有真机巡检留档**（`docs/evidence/*/live-patrol*.txt` +
`patrol-live*.txt`，共 **14 份**）里的失败腿：

| 读数 | 值 |
|------|----|
| 出现 401 / 403 / 429 的巡检留档 | **0 份** |
| 出现过的失败腿 | 2 条，全来自同一次：`[FAIL] oecd/economy_cpi: OecdProviderError: OECD_HTTP_ERROR`（C20 那跑 16:26） |
| 那条失败腿带的可分类字段 | **无**（当时 oecd 的错误不带 status、不带 URL） |

⇒ 三件事同时成立：**分类表不能靠观测来建**（观测里根本没有 401/403/429），
而**历史唯一那条真失败恰好是信息最少的那种形状**，而这类形状在 C21 之后已经不存在了
（今天 `OecdProviderError("OECD_HTTP_ERROR", status=…, url=…)` 两个字段都带，`opendata/data/providers/oecd/models/_client.py:137`）。
所以本轮的分类面是**契约级**的：每条目从 provider 的 raise site 读出来（14 条 fuyao `category`、
11 条本地请求码后缀、2 条未配置码后缀、1 条空响应码后缀、12 个传输异常名、6 条状态码规则
（401/403、429、5xx、404、其余 4xx、**其余不判**）），
不是从留档里统计出来的。这一点在 §6 用退出码钉住，而不是靠一句话声明。

## 2. 判据底稿：只读结构化字段，文案只兜底，兜底必须锚定

`classify_failure` 的取值顺序是判据本身，不是实现细节：

1. `category`（fuyao 树的结构化字段，14 条映射）→ 2. `code` + `status`（`*ProviderError` 家族）→
3. **异常类型名**（12 个传输异常：`ConnectError`/`ReadTimeout`/`RemoteDisconnected`/`SSLError`…）→
4. 状态码兜底（401/403→凭证被拒，429→配额，5xx→源侧，404→没数据，其余 4xx→本地请求）→
5. **文本兜底**，且只匹配 transports 真正写出的那三种形状。

两条设计约束是量出来才写下的：

- **401 优先于码**。`error_for_transport("http", detail="401")` 的 category 是 `http`（会被读成源侧），
  但 401 是凭证事实；所以 `_status_of` 先跑，命中 `_CREDENTIAL_STATUSES` 即返回，
  用例 `test_a_refused_status_outranks_a_code_naming_another_cause` 钉住这条优先级。
- **文本兜底不锚定就会把行数控成状态码**。首版按 `\d{3}` 找状态，于是
  `RuntimeError("42 rows, 1001 columns")` 被读成 402（`local-request`，处置方=本地搬运层）——
  一个凭据面最常见的假绿形状：**把无关数字归因给某个人**。改成锚定三种真实文案形状后
  同一条输入判为 `unclassified`（`test_a_row_count_in_the_text_is_not_read_as_a_status`）。

## 3. 九类、五档、九个署名：覆盖面是逐类点名的，不是"支持多种错误"

| 类 | 档位 | 处置方 | 为什么必须是这一档 |
|----|------|--------|--------------------|
| `not-configured` | alert | 部署 | 没配 Key 是发布缺口，不是数据缺口 |
| `credential-rejected` | alert | 凭据负责人 | 401/403：在场≠有效，这里才需要动 Key |
| `quota-exhausted` | warn | 容量/采购 | 429 不是封禁，找人重发 Key 是错的动作 |
| `source-degraded` | info | 上游源侧 | 5xx 没人能替它修 |
| `transient-blip` | info | 无需处置（抖动） | 与 `source-degraded` 同族但**不同类**：见 §4 |
| `no-data` | info | 业务口径 | 3001/空响应：该问的是口径，不是凭证 |
| `local-request` | info | 本地搬运层 | 400/裸码解析失败：是我们这边 |
| `patrol-gap` | info | 巡检自身 | 探针没配参，是 patrol 的缺陷 |
| `unclassified` | warn | 未定（需补分类） | **读不出来必须是页面上最响的一行** |

`patrol-gap` 判 info 而不是 alert，是 C16 那条口径的延续：patrol 自己的缺口不能算成源的故障
（否则一条未配参的腿会削弱 `auto` 路由）。`unclassified` 判 warn 是同一条的反面：**没归因成功这件事本身要出声**。

## 4. 三条反向守卫：把"最想让它们合并"的两对分开

判据的失败模式不是「分不出」，是「**图省事合成一类**」。所以三条守卫各守一对：

| 守卫 | 用例 | 若被合并会怎样 |
|------|------|----------------|
| 瞬时 ≠ 封禁 | `TestBlipIsNeverABan`：`transient-blip` 不入 `CREDENTIAL_CLASSES`、不点名处置方为凭据负责人 | 一次公网抖动 ⇒ 有人去轮换 Key |
| 封禁 ≠ 瞬时 | `TestBanIsNeverABlip`：401/403 必为 credential 类、必到 alert | 真封禁被读成"等等看"，而 routing 已把源摘了 |
| 401 ≠ 没数据 | `TestRejectedKeyIsNotMissingData`：双向（401 不判 `no-data`，空响应不判凭证类） | 被拒当"确实没有"，于是没人补 Key，报表长期少一个源 |

「401 ≠ 没数据」这半是本轮最值钱的一条：AC-19 的原文把「封禁」和「配额」并列在同一格里，
而它们在数据面上的表现都是「这一格没有数」。**分开之后处置方从「业务口径」变成「凭据负责人」**，
这才是"告警生效"的可操作含义。

## 5. 在场 ≠ 有效：把没做的部分写进载荷，而不是写进 README

`UNVERIFIED_WITHOUT_ACTIVE_CHECK = ("key-validity", "key-expiry", "revocation-or-ban", "quota-left")`
是每条 `KeyReport` 的必填字段，不是注释。真机那一跑（`patrol-live.txt`）里 ths 的行是：

```
ths: presence-only｜ths 已配置 Key 且本轮巡检未见失败；本轮没有到期/封禁主动探测，也未见任何 429，所以本行不等于 Key 有效
```

`presence-only` 是**第五个档位**，它和 `alert`/`warn`/`info`/`not-applicable` 并列，专门用来占住
「配了 Key 但没验过」这一格。它被两条用例钉住不许滑走：`test_a_configured_key_never_probed_is_presence_only`
（配了 Key ⇒ 只能是 presence-only）与 `test_presence_only_never_counts_as_something_to_act_on`
（presence-only ⇒ 既不 alert 也不 warn，且 `owner is None`）。
⇒ 这条档位是**双向**的：既不能被读成"已复核"，也不能被读成"待处置"。

**到期为什么不做**：Key 服务无到期字段主动源（ths/fuyao/fred 三家都没有公布到期日的接口），
本轮没有为了填这一格去猜一个"看起来像到期"的字段。它继续作为演进项留在载荷里。

## 6. 密钥不进报告：一条恒真的泄漏检查，被抓在自己档里

NFR-5 的面上，最初版脚本只有一句「载荷里没有 Key 值 ⇒ 通过」。它**永远不会红**：
如果注入的形状本来就不带 Key，`False` 是白拿的。这就是 C27 刚删掉的那类判据的泄漏版本。

修法是**双向读**（`graded-shapes-render.py`，先数注入前有几条真带 Key）：

| 读数 | 值 |
|------|----|
| 注入前文案含 Key 值的形状 | **5 条**（fred 401/429、httpx 429、requests 502、imf 文案直写） |
| 注入前文案含 `api_key=` query 的形状 | **4 条** |
| 脱敏后文案仍含 Key 值 | **0 条** |
| 分级载荷（JSON）含 Key 值 / 含 `api_key=` | False / False |
| 退出码 | `exit=0`；**若注入面不含任何 Key 值则 `exit=2`**（`LEAK_CHECK_VACUOUS`） |

那条 `exit=2` 不是摆设：把 5 条带凭据的形状剔掉后单独跑了一遍，
得到 `控制组形状数: 12 → vacuous 时的退出码 = 2`（复算命令见 §9 第 4 行）。

**两处真实缺陷是被这条判据逼出来的**：
1. `redact` 只做 URL→origin，抓不到**写在文案里的 Key**（`Authorization: Bearer <值>`），
   于是加"值 pass"（`REDACTED_MARKER`）：本机自己持有的 Key 值参与替换，
   由 `key_values()` 读**字段**而非打印字段（`patrol()` 每跑一次取一次）。
   空值不参与替换（`test_an_unconfigured_key_is_not_a_scrub_pattern`：否则 `"boom"` 会被 `" "` 类空串切碎）。
2. 归档打印行原来是 `message[:110]`——**一个用截断来证明不泄漏的档案**。
   改成打印全文，并让退出码承担判定（截断只能藏泄漏，不能证明没有）。

## 7. 机制：判定面唯一，三个消费方共用一份 `key_health.py`

C27 那句「测量面可以独立，判定面必须唯一」本轮是照着执行的：分类只发生在 `classify_failure` 一处，
`credential_health()` 只做一次 join（`key_status()` 的在场事实 + 观察到的类），三方消费——

| 消费方 | 入口 | 改了什么 |
|--------|------|---------|
| 巡检 catch site | `opendata/pipeline/patrol.py` | `PatrolResult` 多两个字段（`failure_class` / `attribution`），error 文本过 `redact` |
| cron 运维档 | `scripts/ops/health_patrol.py` | 新增「== 凭证分级（AC-19）==」一节；**退出码语义一字未动** |
| 两个公共端点 | `opendata/api/pipeline.py` | `_graded_health()` 一处渲染，`/health/sources` 与 `/health/patrol` 都走它 |

`_graded_health` 是计划外的一条：第一条端点改动直接内联了一个推导式，105 字符被 ruff E501 拦下，
提成函数后才发现**两个端点各写一份渲染**正是 C27 那种「同一事实两处陈述」——于是第二处也改成调它。
⇒ 现在两个端点对同一个 Key 不可能给出不同档位，也不可能返回一个"可能渲染出多余东西"的对象
（返回的是 `as_dict()`）。

## 8. 明确拒绝的六种"让它过去"

1. **把 fred 的 `not-configured` 算成 info**。真机那一跑的 alert 计数就是 1（fred 缺 Key，`必须=True`）。
   它是部署缺口，本轮没有 Key 可以配（也不该去配），所以它**继续红着**：`PATROL_EXIT=0` 的
   退出码规则一字未改，摘要里写「必配 Key 缺失 1 项；凭证告警 1 项」。
2. **用文案正则把状态码补齐**。`_status_from_text` 只匹配 transports 真实写出的三种形状，
   匹配不到就判 `unclassified`。给它加一条宽松的 `\d{3}` 能立刻让 unclassified 变少——
   那正是 §2 那条「把 42 rows 判成 402」的缺陷的复发生长方式。
3. **给无 Key 的源（ecb/yfinance/imf/oecd/akshare）跳过分级**。分级面按观察走：
   ecb 的 403 也是访问问题（`required=False` 在读数里显示「无 Key 源」而不是压制这条 alert）。
   这是本轮一个**有意的设计选择**而不是疏漏，写在这里是因为它看起来像误报。
4. **把 `unclassified` 收进 `source-degraded`**。收进去之后告警面更"干净"，代价是
   新出现的失败形状会被读成一个已知的、有人认领的类 ⇒ 分类表永远不会暴露自己过期。
   离线归档里 oecd 那条历史形状**故意保留**为 unclassified，用来显示这一格真的会亮。
5. **让分级参与 routing**。401 到底该不该把源从 `auto` 摘掉？本轮**不动**，
   并用参数化用例反向钉住（`test_grading_never_moves_the_routing_mark`：401/429/503 三种形状
   都仍然让 `source="auto"` 落到 `LookupError`，与分级之前逐字相同）。
   理由：routing 的语义是「这个源现在能不能给数」，而「凭证被拒」的答案是**换一个源但数据仍然可达**，
   两件事合成一个位就会有一件说谎。这属迭代 2 的账（§10 第 3 条）。
6. **补一条"看起来像到期"的探测**。没有任何源公布到期日，猜一个字段就是把 §5 那句话擦掉。

## 9. 证据清单

| 文件 | 内容 | 判据 / 退出码 |
|------|------|-------------|
| `graded-shapes-render.py` / `.txt`（88 行） | 离线注入 **19 条已录制形状** → 9 类逐类读数 + 与 cron 同一形状的分级渲染 + **双向**泄漏检查（§6 表） | `exit=0`；`LEAK_CHECK_VACUOUS` 时 `exit=2`（已单独复算触发过一次）；零网络、不写数仓 |
| `patrol-live.txt`（61 行） | 真机巡检（只读探测）：**21 条腿全 `[ok]`**、凭证面三档齐全（`ths: presence-only` / `fred: alert not-configured×1` / 5 个无 Key 源 `not-applicable`） | `exit=0`；本轮现场**未见任何 401/403/429**，这条负读数与 §1 的分类表面同样重要 |
| `judge-tests.txt`（166 行） | 判定面两条测试文件逐条 `-v` 复算（`--no-cov` 是部分跑既定口径） | **136 passed**、`exit=0` |
| `judge-tests-before-six-cases.txt` / `gate-run1-before-six-cases.txt` | 两份**被替换的**前跑（130 例 / 2,757 例全门），档尾各补一段说明它为什么不再是最终判定面（不改写上面任何一行） | 130 passed / `GATE_EXIT=0` |
| `tests/test_key_health.py`（新增 75 例） | 分类/守卫/载荷全部判据（§3–§6 的每条具名用例都在这里） | 门禁内跑 |
| `tests/test_patrol.py`（49 → **61 例**，+12） | 分级落在真巡检链路上的 10 条（catch site 写类与归因、被救回的腿保留失败那一类的归因、`key_values` 只读两个字段、routing 标记不动的 4 例参数化反向守卫）+ 端点面 2 条 | 门禁内跑 |

本轮的"能红"不靠断言数量，靠三处独立读数：**同一份判据在离线注入面（§6，能 `exit=2`）、
真机面（§5，`presence-only` 那一行是现场读出来的）、门禁面（§9 的 136 例）各自说同一件事**。

## 10. 判据未放宽的声明

1. **AC-19 的 box 没有勾**，§2 清单里那条仍是 `- [ ]`：到期未做、且验收面（勾 box）不属本轮权限口径。
2. **没有为了分类而改任何 provider 的错误语义**：`ths` 那条改动只是把 `message.split(":")[0]` 存进
   `self.code`（原本只在前缀里、无人取用），其余 provider 一字未动；`ThsProviderError` 的文案不变。
3. **routing、退出码、skip 语义全部未动**：`health_patrol.py` 的 `return 1 if failed or deviations else 0`
   一字未改（分级只是多打一节）；§8-5 那条参数化用例是这条声明的判据面。
4. **没有加豁免**：`pyproject.toml`、`bandit.yaml`、`docs/quality/`、`scripts/quality/` 的 diff 为空；
   棘轮五项与 C27 逐条相等（三条 improved 未 `--update` 冻结）。
5. **key_health 剩余未覆 3 行是设计上门不可达的**：`47`（`if TYPE_CHECKING` 导入块，C8 起全库同口径）、
   `596-597`（`_level_rank` 收到未知档位的防御分支，档位只有 5 个且都来自 `CLASS_LEVELS`）、
   `295->298`（fuyao category 存在但不在 14 条映射里的落空弧，今天无此类 category）。
   本轮把 8 行未覆降到 3 行的那 6 条用例，每条都对应一个**能说出后果**的判据（§2、§5、§6），
   没有一条是「为了走到的行」。
6. **`key_values()` 是一个返回凭证值的公共函数，这一点是有意的**：它唯一的用途是给 `redact` 当替换模式。
   收成私有当然更安全，但那会让本轮的公共 API 计数（466）少一个、并要为此重跑一次全门；
   真正决定风险面的是本仓库**早有同类**——`ths/models/_client.py` 的 `credentials()` 自 C13 起就是公共函数、
   返回的是凭证对象本身。所以这条不是新增一类暴露面，而是与既有口径一致的第三个读取点；
   它不进任何返回值（`patrol()` 不返回它、两个端点不调它），且由 `test_key_values_reads_the_credential_fields_and_nothing_else`
   钉住「只读这两个字段」。

## 11. 门禁与测试数字

| 面 | C27 基线 | C28 第 1 跑 | C28 最终（第 2 跑） |
|----|---------|------------|---------------------|
| `make gate` | `GATE_EXIT=0` | `GATE_EXIT=0` | **`GATE_EXIT=0`**（`gate.txt` 6,239 行，末行读数） |
| `pytest -n 8 -m "not e2e"` | 2,676 passed / 6 skipped（51.12s） | 2,757 / 6（50.86s） | **2,763 / 6（51.15s）** |
| 覆盖率（阈值 84% 未动） | 86.49%、TOTAL 10,730 / 未覆 1,280 | 86.69%、TOTAL 10,942 / 未覆 1,288 | **86.72%、TOTAL 10,942 / 未覆 1,285 / 分支 2,692 / 部分 305** |
| `opendata/pipeline/key_health.py` | —（本轮新增） | 95.08%、188 语句 / 未覆 8 / 分支 76 / 部分 5 | **98.11%、未覆 3、部分 2**（缺的正是 §10-5 那三处） |
| a2-check | 274 文件 | 277 文件 | **277 文件**，ruff + format + mypy + bandit 四项全 `ok` |
| public API | 459/459 双 100% | 466/466 双 100% | **466/466 双 100%**（+7 = `key_health` 的公共判据函数） |
| 棘轮五项 | 274/2144/1044/21/4（快照 277/2145/1045/21/4） | 同 C27 | **同 C27，三条 improved 未 `--update` 冻结** |
| 前端 | 8 files / 79 tests | 8 / 79，eslint 0 errors / 48 warnings | **8 / 79（3.04s）**，本轮无前端改动 |

用例增量完全对上：**+81 = 69（`test_key_health.py` 首跑 69 例）+ 12（`test_patrol.py` 49 → 61）**，
第 2 跑再 +6 = §6/§10-5 那三条判据的展开（3 个不认识的档位码 + 2 个畸形 URL + 1 条配额边界句），
`2,757 → 2,763` 就是这个 +6。该文件补完后共 **75 例**。
`6 skipped` 与 C27 **逐字同批**（`tests/test_port_fidelity.py` 的 4 例 em 夹具 + 两条新判据的 `[em]` 参数），
本轮一条都没动过它，也没借"本轮无关"把它读成不适用。

**两跑对照（同一 HEAD `c062580`、198 个覆盖文件）**：判定面的差异只有两行——
`key_health.py` 未覆 8 → 3、部分分支 5 → 2（本轮补的那 6 条用例的去处），
以及 `opendata/api/data.py` 未覆 2 → 4、部分 2 → 3。
**后一行本轮代码里一字节未动**，两次连跑换了读数，
这正是 C27 §10 第 7 条登记的那条**时序依赖观测**（后台任务是否赶在 TestClient 拆除前抛错／被取消）
第一次被两次连续门禁跑直接抓到：第 1 跑是 `70, 141, 187->189`（26 份留档里最常见的那个读数），
第 2 跑是 `67, 70, 140-141, 187->189`（C27 那跑的读数）。
⇒ 处置仍然是不处置：不为此调阈值、不写成"本轮修复"、也不写成"本轮回归"，只登记；
`TOTAL` 未覆 1,288 → 1,285 的净 −3 完全由这两行组成（`−5 + 2`），其余 196 行逐字不变。

## 12. 遗留（明确不做 / 做不了，含原因）

1. **Key 到期仍然未做**（AC-19 演进项原文的后半）。三家有 Key 的源都没有公布到期日的接口，
   唯一可行路径是**日历侧**（自己记颁发日 + 采购周期），那是运维台账不是数据面判据，属迭代 2。
2. **配额余量只有 429 这一种证据**。载荷里那句「配额证据只有 N 次 429 本身，无余量主动源」
   是这个边界的机器可读形式（§5、§10-5）；fred/ths 若哪天给出 `Retry-After` 或余量响应头，
   分类面需要新增一个类而不是复用 `quota-exhausted`。
3. **分级不参与 routing**（§8-5）。真做到"凭证被拒 ⇒ 换一个源"需要区分「源不可达」与「源不可用」，
   而健康位只有一个布尔——这条与 C19/C25 登记的「字段塌陷不该摘源」是**同一个位被要求表达两件事**，
   应一并设计，不单独在本轮动。
4. **`patrol-gap` 只有一类一档**：探针装配失败（`PatrolProbeConfigError`）之外的 patrol 自身缺陷
   （字段级 canary 读不到那一页）现在仍走 C25 的 `no-reading` 口径，没有并入这一类。
5. **`unclassified` 的处置方是「未定」** ⇒ 它告警但没人认领。这是刻意的：本轮没有为了让这一格好看
   而指定一个 owner。它红到什么程度算吵，需要一次真实 cron 运行的观察期。
6. **19 条注入形状仍不包含 em 通道的形状**（`push2delay` 502 一类只在 AC-6 的挂账里）。
   本轮把它读成 `source-degraded`（离线注入那条），但**没有真机 429/401 的任何一条留档**——
   §1 那个「0 份」是要被补的，补法是等一次真故障，不是造一次。
