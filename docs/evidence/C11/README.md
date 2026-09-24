# C11 轮：em 通道补录尝试 + 搬运层静默空帧缺陷（C11a）修复

日期：2026-09-25（北京时间凌晨）｜分支：`dev`｜前置：C10（`e6afe9b`）
本轮两条线：**（一）** 尝试补录 AC-6 挂起的 4 个 em 用例；**（二）** 顺着补录过程中暴露的
现象，审计并修复搬运层"把上游拒绝吞成空帧"的静默欠抓缺陷。

---

## 1. em 通道补录：仍不可达（量化表征，非结论性放弃）

`python scripts/codemod/compare_with_upstream.py --record --only stock_daily_raw
--only stock_daily_qfq --only index_daily_em --only fund_etf_daily_em`

| 用例 | 实测失败形态 | 夹具状态 |
|------|-------------|---------|
| `stock_daily_raw` | `HTTPError: 502 Bad Gateway`（`push2delay…/stock/kline/get`） | pending（原因戳刷新为本次观测） |
| `stock_daily_qfq` | 同上，502 | pending（同上） |
| `index_daily_em` | `EmptyReferenceFrame: upstream returned 0 rows`（上游侧同样取不到） | 新建 pending 夹具 |
| `fund_etf_daily_em` | `RuntimeError: Eastmoney ETF history endpoint request failed: push2his…` | 新建 pending 夹具 |

三种形态互不相同，说明这不是单一故障点：**push2his 直接拒（RemoteDisconnected / RuntimeError）、
push2delay 返 502、且 curl 单发可用一次后再发即 `Empty reply from server`**——连接级突发限流。
其中 `index_daily_em` 的 `EmptyReferenceFrame`（**参照侧** pristine akshare 也返回 0 行）不是巧合：
pristine `index_zh_a_hist` 把同一个 502 吞成空帧，所以"上游没数据"这个表象本身就是 §2 缺陷的现场证据。
01:32 起挂 30 次 × 60 s 探测脚本，至 probe19（01:52）`push2delay` 仍 502/空回。
**判据不放宽**：4 个用例保持 `pending`，AC-6 计数仍为 **12/16 PASS**。

**回归确认**（离线回放，与补录无关的既有夹具）：`--compare` 仍 **12 PASS / 0 diff**，
含 `index_constituent: 300 rows, 0 diff(s)`——该用例正好穿过本轮被改动的搬运函数，
证明改动未改变成功路径的字节级结果。报告：`docs/evidence/A2/compare-report.md`。

## 2. C11a 缺陷审计：10 个被 fetcher 调用的搬运函数逐个判定

判据只有一条：**上游在传输层拒绝时，调用方能否看出来**（`opendata/data/providers/akshare`
下 10 个 fetcher 的 `extract_data` 实际调用的函数全集，AST 扫描函数体取调用点与 try/except）。

| 搬运函数 | 传输调用方式 | 拒绝是否可见 | 处置 |
|---------|-------------|------------|------|
| `stock_zh_a_hist` | `request_eastmoney`（内部 re-raise） | ✅ | — |
| `fund_etf_hist_em` | `_get_eastmoney_fund_json` → `RuntimeError` | ✅ | —（正是 §1 的观测形态） |
| `stock_financial_report_sina` | `requests.get`，无 try | ✅ | — |
| `stock_financial_analysis_indicator_em` | 同上 | ✅ | — |
| `stock_history_dividend_detail` | 同上（4 个调用点） | ✅ | — |
| `futures_zh_daily_sina` | 同上 | ✅ | — |
| `bond_zh_hs_cov_daily` | 同上 | ✅ | — |
| `option_sse_daily_sina` | 同上 | ✅ | — |
| `index_zh_a_hist` | `requests.get` + `except: continue`，候选全失败后 `return 空帧` | ❌ **静默** | **本轮修复** |
| `index_stock_cons_weight_csindex` | `except` / 非 200 / body 不可解析 三种失败均 `return _empty_…()` | ❌ **静默** | **本轮修复** |

后果说明：`index_daily` / `index_constituent` 两条腿在 em 拒绝时返回 `[]`，增量调度无法把
"上游拒绝"与"窗口内确实没有数据（周末/节假日）"区分开，于是**静默欠抓且不告警**——这正是
AC-13 记录的「15:00 当日 K 未返回」现象的唯一可解释路径（`docs/evidence/B4/`）。

## 3. 修复设计：区分「拒绝」与「空窗」，而不是把空结果一律当错误

- `index_zh_a_hist`：改走 `request_eastmoney`（复用本仓库既有的 push2delay / curl 回退通道，
  与 `stock_zh_a_hist` 同一条路），并区分两种"没有 klines"：
  **所有 secid 候选都没能拿到一个可用响应 → `raise RuntimeError`（保留 `__cause__`）**；
  只要有候选正常应答但数据为空 → 仍返回空帧（合法空窗）。
- `index_stock_cons_weight_csindex`：传输失败、非 200、body 无法按 XLS 解析三种形态各自抛
  `RuntimeError`（消息含 symbol 与 URL）；解析成功但零行仍返回空帧。
- 未采用"空帧即错误"的粗判据：那会把周末/节假日窗口变成误报。修完后**不再需要**先补交易日历
  谓词才能判定欠抓（§6 修订了此前的规划）。

### 真机对照（`fail-closed-probe.txt`）

| 探测 | 结果 | 判读 |
|------|------|------|
| `index_zh_a_hist("000300", 20240101..20240331)` | 25.7 s 后 `RuntimeError`，`cause=HTTPError 502 push2delay…secid=47.000300` | 改前：同一时刻返回 0 行（静默）。4 个 secid 候选 × 重试 × 回退全部走完才判定拒绝 |
| `index_stock_cons_weight_csindex("000300")` | 1.8 s，300 行，`日期=2026-08-31`，权重 0.433/0.081… | 成功路径未被改成误报 |

## 4. 可重放性与登记（搬运层改动必须能被 codemod 重放）

| 项 | 结果 |
|----|------|
| `scripts/codemod/port_module.py::MANUAL_EDITS` | +2 条登记（4 个 + 1 个正则 transform），逐条 `re.subn` 命中 1 次 |
| 重放 vs 树内容 | `python scripts/codemod/report_port.py` → **`0 pending TODOs`**（逐字节一致，上游同步不会丢补丁） |
| `opendata_http/upstream.lock` | 两文件 `manual_edits: false → true`（人工改动 6 → **8** 个文件） |
| `opendata_http/manifest.json` | 再生（313 py + 2 资源 / 93,199 行） |
| `THIRD_PARTY_NOTICES.md` 人工改动登记 | 拆为"凭证脱敏 5 处 / 行为修正 3 处"两张表，并写明"合法空窗仍返回空帧"的共同判据 |
| 顺带修正 | `apply_manual_edits` 原会对任何登记改动强塞 `import os`（`datasets.py` 里因此留了一个死 import，搬运树不在 `PY_PORTED` 的 `E,F` lint 范围内所以一直没被发现）→ 改为仅当 replacement 引用 `os.` 时注入；并删掉 `datasets.py` 的死 import，使其重放重新逐字节一致 |

## 5. 测试与门禁

- 新增 `tests/test_port_fail_closed.py`：**10 条**（两条腿各覆盖 传输拒绝 / 非 200 / 坏 body /
  合法空帧 / 正常解析，外加 fetcher 层"必须上抛而不是返回空"与补丁存活守卫）。
  先以现状跑为 **7 failed / 3 passed**（缺陷复现，3 条 green 是既有正确语义被钉住），改后 **10 passed**。
- `tests/test_port_module.py` +2 条：非凭证类补丁不得注入 `import os`；两个补丁已登记可重放。
- 相关定向套件：`test_port_module / test_port_fail_closed / test_port_fidelity / test_p0_providers /
  test_p1_daily_fetchers` 全绿；`ruff check` + `ruff format --check` 对改动文件干净。
- 全量门禁见 `gate.txt`（完整未裁剪）。

## 6. 遗留（明确不做 / 做不了，含原因）

1. **em 4 个挂起用例待网络恢复补录**（任务 #14）：判据不放宽；恢复后可直接
   `--record --only …` × 4 再 `--compare`。
2. **`index_code_id_map_em()` 仍把异常吞成 `{}`**：它只是 secid 提示表，候选列表随后仍会逐个试；
   现在全部候选被拒会抛错，所以不再有"掩盖丢数"的路径。
3. **sina 腿的"200 + 反爬 body"形态未逐个判定**：需要"该窗口应有多少行"的谓词（交易日历 +
   标的上市区间）才能与真空窗区分，登记到 AC-13 待办，不在本轮放宽判据硬猜。
4. **fred 腿仍 `verified=false`**：本环境 `FRED_API_KEY` 未设置，客户端 fail-closed
   （`FRED_API_KEY_MISSING`）；不为通过验收而切到无鉴权 CSV 通道。
5. **`fund_etf_daily` 的 ths 腿仍不注册**：上游 ETF K 线只给前复权序列（C6 实测），D10 禁止入库。
6. **ths 交易日历语义已实测更正**（本轮附带）：`/api/a-share/calendar/trading-days` 一次只给
   **滚动一年**（2025-09-25..2026-09-24，且不含当日），`start_date/begin_date/start/year/days/limit`
   六个候选参数全部被忽略 → 可算 `prev_trade_date`，**算不了**未来 `next_trade_date`，
   也不能用于历史回填的日序还原。`opendata_fuyao/endpoints.py` 的 docstring 已按实测改写。
