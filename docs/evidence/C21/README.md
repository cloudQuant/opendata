# C21 轮：巡检告警面（失败归因 + 抖动显式化）与门禁暴露的搬运层时区缺陷

日期：2026-09-25｜分支：`dev`｜前置：C20（`8f56d45` 之前的 ETF 日线腿）
本轮两条线：**（一）** 把 C20 留下的"告警面读不出结论"修掉——4 个公共 HTTP provider 的失败带
`status` + 请求 URL，巡检对抖动重试一次但**显式标出"是靠重试才过的"**；**（二）** 本轮第一次门禁
跑挂，挂的不是巡检面而是 AC-6 保真回放，根因是**搬运层按本机时区渲染时间**，一并修掉。

---

## 1. 缺陷一：巡检日志读不出"该找谁"

C20 的两跑（`docs/evidence/C20/live-patrol.txt` / `live-patrol-rerun.txt`）留下这样的告警：

```
[FAIL] oecd/economy_cpi: OECD_HTTP_ERROR
```

四条腿（oecd / ecb / fred / imf）的错误消息**只有一个码**，既没有 HTTP status 也没有 URL。
于是"上游那一刻没答好"与"我们的请求写错了"在日志上不可区分——C20 是靠**手写探针重放 12 轮**
才归因出来的（见 `docs/evidence/C20/oecd-flakiness-attribution.txt` 的方法更正）。
另外，一次瞬态失败直接把腿标成不可达（`registry.mark_unavailable`），auto 路由随即绕开该源。

判据取自仓库既有约定，而非新发明：受治理的客户端 `opendata/data/http_client.py:52 HttpFetchError`
就渲染 `host=… status=… attempts=… url=…`。四个 provider 包刻意互不依赖，所以**不抽公共基类**，
按各自错误类补同样的字段。

| provider | 补入的请求面 | 消息形态（实测） |
|---------|-------------|----------------|
| oecd | `{base}/data/{flow}/{series_key}` | `OECD_HTTP_ERROR status=403 url=https://sdmx.oecd.org/public/rest/data/OECD.SDD.TPS,DSD_PRICES@DF_PRICES_HICP/NO.SUCH.KEY` |
| ecb | `{base}/service/data/{series_key}` | `ECB_HTTP_ERROR status=400 url=https://data-api.ecb.europa.eu/service/data/ICP/NO.SUCH` |
| imf | `{base}/{indicator}/{country}` | `IMF_BAD_RESPONSE status=200 url=https://www.imf.org/external/datamapper/api/v1/NO_SUCH/USA` |
| fred | 只到 `…/fred/series/observations`，**key 作 query 参数不入消息** | `FRED_HTTP_ERROR status=429 url=…`（`api_key` 值不出现在消息里，有用例钉住） |

行级失败码（`*_BAD_OBSERVATION` 这类"答了但某行不合格"）**保持裸码**：它没有单一请求可归因。
未配 Key 的 fred 只报 `FRED_API_KEY_MISSING`——没有发生请求，就没有可归因的 URL（判据 C）。

## 2. 缺陷一（续）：抖动重试一次，但绝不把抖动染绿

`opendata/pipeline/patrol.py`：`PROBE_ATTEMPTS = 2`、`PROBE_RETRY_BACKOFF = 2.0`。三条边界都是刻意的：

1. **`PatrolProbeConfigError`（探针没配参）不重试**——第二次读的是同一张参数表，重试只会把
   配置缺陷伪装成网络问题；用例 `test_config_gap_is_never_retried` 断言 `fetcher.calls == 0`。
2. **第二次仍失败照旧 `mark_unavailable`**——重试不是消音器（反向对照 A2）。
3. **靠重试才过的腿保留第一次失败**：`PatrolResult.attempts` + `flaky` 属性（`ok and attempts > 1`），
   日志多打一行 `patrol {leg} passed only on attempt {attempts}: {第一次的错误}`，
   `scripts/ops/health_patrol.py` 单列 FLAKY 段并计入 `失败 X 项；抖动 Y 项；必配 Key 缺失 Z 项`，
   `/health/patrol` 载荷加 `flaky`（聚合）与每条结果的 `attempts` / `flaky`。
   **FLAKY 不翻退出码**：它既不是失败也不是干净通过，翻退出码就等于把抖动当故障、把重试当消音器。

## 3. 缺陷二：门禁第一次跑挂 —— 同一列差整 5 小时

```
FAILED tests/test_port_fidelity.py::test_ported_output_matches_upstream[financial_statement]
AssertionError: cell 更新日期[0]: '2026-08-14T20:50:08' != '2026-08-14T15:50:08'   （10/10 行同向）
```

左侧是夹具参照值、右侧是本轮回放值。本轮改的代码完全没碰 `financial_statement`，
差别在于**机器时区**：C20 那几跑时本机是 `+0800`（AC-6 夹具也是那时录的），本轮已是 `+0300`。
根因在搬运层（及其上游）：`opendata_http/stock_fundamental/stock_finance_sina.py` 用
`datetime.fromtimestamp(update_time)` **不传 tz**，取本机时区渲染一个语义上是北京时间的 epoch 秒。

**没有采用的两种"能过"的修法**：① 按 `+0300` 重录夹具（等于把判据搬到当下环境去迁就）；
② 在测试里设 `TZ=Asia/Shanghai`（把缺陷留在生产路径上，只让测试好看）。
采用的修法：把渲染基准**钉在数据语义所属时区**，于是回放结果与 `+0800` 录制值重新逐字相等，
且换任何一台机器都一样——判据一个字没松，只是不再依赖执行它的机器。

| 项 | 结果 |
|----|------|
| `MANUAL_EDITS` 登记 | **+1 条**（3 个正则 transform，逐条 `re.subn` 命中 1 次），与 C11a 同一机制 ⇒ 上游同步重放不会丢补丁 |
| 重放一致性 | `scripts/codemod/report_port.py` → **315 files, 0 pending TODOs**（树内容与确定性重放逐字节一致） |
| `upstream.lock` | `stock_fundamental/stock_finance_sina.py` `manual_edits: false → true`（人工改动 **8 → 9** 个文件） |
| `manifest.json` | 再生（313 py + 2 资源 / 93,205 行） |
| `THIRD_PARTY_NOTICES.md` | 行为修正表 3 处 → **4 处**，并写明第四处属另一类缺陷（时间语义随时区漂移，不是吞错） |
| 契约影响 | 无：`normalize()` 把 `更新日期` 当页元数据丢弃（`_METADATA_COLUMNS`），字符串形态与列名都未变 |

## 4. 证据清单

| 文件 | 内容 | 判据 / 退出码 |
|------|------|-------------|
| `live-patrol.txt` | 真机全量巡检第一跑（13:53 `+03:00`）：20 条 verified 腿逐条 `[ok]` + 凭证配置面 | `失败 0 项；抖动 0 项；必配 Key 缺失 1 项`，`PATROL_EXIT=0` |
| `live-patrol-rerun.txt` | 同一命令第二跑（自然复现） | 同上，`PATROL_EXIT=0` |
| `flaky-retry-attribution.py` / `.txt` | 真机注入归因探针：**只在最底层 `oecd._http_get` 打一个带标签的 `ConnectError`**，patrol/registry/provider 全走生产路径 | A1 一次抖动 → `attempts=2 ok=True flaky=True rows=24`，auto 仍可路由；A2 两次抖动 → `ok=False flaky=False` + 该腿被标不可达（**反向对照**）；B 三个 provider 各发一个上游不认的键 → 真实 status + 无 query 的 URL；C fred 未配 Key → 只报裸码。三条判据全 PASS，`PROBE_EXIT=0` |
| `update_time-tz-pin.py` / `.txt` | AC-6 时区固定的离线证明：同一份录制应答在 Asia/Shanghai / Etc/UTC / America/Los_Angeles 三档主机时区下，**上游原式**给出 `20:50:08 / 12:50:08 / 05:50:08`（漂移），**固定后的搬运层**三档一律 `20:50:08` = 录制值 | 三条判据 PASS，`PROBE_EXIT=0` |
| `gate-before-tz-fix.txt` | 本轮**第一次**门禁完整未裁剪输出（5,744 行）；抬头是 `#` 单行式（`git status --porcelain` 用 `\|` 串在一行，另有 `node vv25.1.0` 的重复前缀笔误），与 `gate.txt` 的 printf 逐行式抬头不同——两件的**正文**都是未裁剪原始输出，只是抬头框架先后不同，读数以文件内的 `GATE_EXIT=` 为准 | `GATE_EXIT=2`，`1 failed, 2580 passed, 5 skipped` —— 即 §3 的缺陷现场，原样保留不裁剪 |
| `gate.txt` | 修复后重跑的门禁完整未裁剪输出（5,869 行，抬头含 `本机时区偏移=+0300 缩写=+03`，并注明夹具录制时为 `+0800`）。复跑的原因：本 README 与 `update_time-tz-pin.py` 落在 a2-check 扫面内、且晚于第一份修复后归档落地，归档必须覆盖它们；两跑的判定读数相同（同为 `GATE_EXIT=0`、2582 passed / 5 skipped / 86.27%），只有墙钟耗时与抬头时间戳不同） | `GATE_EXIT=0`，`2582 passed, 5 skipped in 54.12s`，coverage `86.27%`（阈值 84%），前端 8 文件 / 79 测试 |

## 5. 测试与门禁数字

- `tests/test_patrol.py` **25 → 31 条**：新增 `TestProbeRetry` 5 条（抖动被救回且源仍可路由 /
  flaky 保留第一次失败 / 第二次失败仍判不可达 / 干净通过不多打一次 / 配置缺陷永不重试）+
  API 侧 1 条（`/health/patrol` 把被救回的腿报成 `flaky`）；`_instant_backoff` autouse 夹具把
  退避压到 0 s，避免测试真等 2 秒。
- 4 个 provider 套件 **+5 条**：`test_http_error_names_the_failing_request` ×3、fred 的
  `test_http_error_names_the_request_but_never_the_key`（断言 `api_key` 值不在消息里）、
  fred 无 Key 路径断言裸码 `FRED_API_KEY_MISSING`。
- `tests/test_port_fidelity.py` **+1 条** `test_financial_statement_update_dates_ignore_the_machine_timezone`：
  同一份录制在三档时区回放必须给出同一列且等于录制值。**非自证验证**：把搬运文件回退到修复前
  （`git checkout` 后由 codemod 重放恢复），该用例与 `[financial_statement]` 一并 FAIL
  （`Etc/UTC rendered 更新日期 differently from the recording: 12:50:08 != 20:50:08`）。
- 顺带收口：`tests/test_port_fidelity.py` 被拉进 A2 零容忍集后补齐了 4 处历史缺失类型标注
  （mypy strict 对整文件生效，这是既有约定而非本轮放宽）。
- 门禁复跑：`GATE_EXIT=0`；AC-6 离线回放 `--compare` 复算 **12 PASS / 4 PENDING / 0 差异**
  （计数未变，变化是一台 `+0300` 机器上原本 FAIL 的用例重新 PASS）。
- 本轮新增分支被覆到的读数（取自 `gate.txt` 的 coverage 表，非估算）：
  `opendata/pipeline/patrol.py` **136 语句 / 6 未覆 / 22 分支 / 1 部分 / 95.57%**，
  未覆区间只有 `33-39`（`if TYPE_CHECKING` 导入块，与 C8 起声明的全库同口径一致）；
  全库 `TOTAL 10603 / 1282 / 2572 / 307 = 86.27%`，阈值 84%。

## 6. 判据未放宽的三处声明

1. **没有为通过回放而重录夹具**，也没有在用例里临时设时区。
2. **重试不是消音器**：A2 反向对照（两次抖动仍判死）与"FLAKY 不翻退出码"同时成立。
3. **两个自然巡检跑都是 0 抖动**，所以 §4 的抖动行为来自**带标签的注入**而不是自然现场——
   这一点在探针输出里显式写明（`【C21 证据注入，非上游真实行为】`），没有把注入包装成真实故障。

## 7. 遗留（明确不做 / 做不了，含原因）

1. **`push2delay.eastmoney.com` 现在连 DNS 都解析不了**（`--compare` 复算时刷新了挂起原因，
   由 `HTTPError 502` 变为 `ConnectionError … Failed to resolve`）：任务 #14 的 4 个 em 用例
   继续 `pending`，不补录、不计入 PASS。
2. **搬运层还有两处同样的"本机时区"渲染**：`akshare/stock/stock_xq.py:27`（雪球现价快照）与
   `akshare/stock_feature/stock_info.py:167,200`（新闻列表时间）。二者**不在任何 fetcher 的调用面上**
   （`opendata/data/providers` 全量 grep 无引用），因此没有夹具可证、也没有消费方会被影响——
   本轮不扩大改动面，登记待有腿接入时一并处理。
3. **宏观三域缺权威序**（任务 #28）：`authority.json` 未覆盖 `economy_cpi` / `economy_gdp` /
   `economy_unemployment`，auto 路由退化为注册顺序决定。本轮实测复现（同一棵树、只换收集顺序）：
   `pytest tests/test_oecd_provider.py tests/test_ecb_provider.py tests/test_imf_provider.py -k Registration`
   → **2 failed**（`test_ecb_provider::TestRegistration::test_verified_capability_is_auto_routed`、
   `test_imf_provider::TestRegistration::test_auto_routing_prefers_a_verified_source`）；
   换 `ecb imf oecd` 顺序 → **9 passed**。这 2 条断言目前是**顺序巧合**在撑着，
   本轮复现并登记，不改判据、不把它们标成 xfail。
4. **akshare 10 条腿 `verified=false` 的口径复核**未做（需要逐域跨 vendor 对照），与 B1.2/B1.3 的
   转正要求一并留在 AC-10。
5. **AC-15 旧表不 DROP**（用户决策：只保留只读）、**AC-19 字段级巡检判据**、
   `dwd_fund_etf_daily` 等四张表的落库，均待用户确认后再动。
