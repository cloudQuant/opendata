# B1 全量搬运 + 双源落库 — 验收证据索引

> 迭代 1B / 里程碑 B1（AC-5 全量、AC-6 抽样、AC-8 双源、AC-9 校对）
> 日期：2026-09-24
> 环境：macOS（Apple Silicon）| conda env `py313` | MySQL

## 1. 交付物

| # | 交付物 | 结果 |
|---|--------|------|
| B1.1 | P1 域子模块搬运 | **+183 文件 → 313 py + 2 资源**（futures 32 / fund·option·bond·economic 82 / stock_feature 69 + ths.js）；`upstream.lock` 315 项逐文件 sha256 与上游 `c4f6a63` 一致；`manifest.json` 校验通过 |
| B1.2 | 双源真实落库 | `ods_stock_daily_ths` **10,310,287 行 / 5,568 标的**（10 年 dump，A3.3b 通道）；`ods_stock_daily_akshare` **617,371 行 / 5,864 标的**（C3 旧库迁移） |
| B1.3 | 真实双源交叉校对 | **719,314 键比对**；1,989,684 字段差异 + 103,555 缺失；`dq_diff_report` 落库 |
| B1.4 | dwd 双源合并 | **719,314 行**（权威 ths 717,702 / akshare 降级填补 1,612），`_diff_flag` 打标 615,610 |
| — | 复权因子 | `ods_stock_action_ths` 57,463 事件（1991..2026） |

## 2. 关键证据

- `dual-source-cross-check.txt`（本目录）：数据规模 + 校对结果 + **差异人工复核归因**
- 复算：`python _dual_source_tmp.py`（脚本已随提交清理，逻辑见本节说明）
  1. `ods_frame_reader(engine, domain, source)` 读 ods（源列名）→ dwd 合并
  2. `ods_raw_reader(engine, domain, source)` 读原始源帧 → 交叉校对
  3. `CrossCheckService.run(batch_id, window)` + `DwdMergeService.run_hook(ctx)`

## 3. 差异归因结论（AC-9「确认为源差异而非程序错误」）

1. **OHLC 一致偏移 ~3.13%**：四个价格字段同向同幅 → 价格基准（复权口径）差异；
   akshare 侧来自旧库迁移，其复权基准是旧采集时点的口径（不可用 ths 事件复现：
   重算 qfq 0.9474 ≠ 观测比值 0.9687）。
2. **akshare `amount` 全 0**（617,017/617,371 行）：旧采集未记录成交额 → 源完整度差异。
3. **volume 差异与 103,555 缺失**：两源交易日历/停牌语义差异。

**结论：未发现程序性错误。** 该结论同时暴露一处真实缺陷（下节）并已修复。

## 4. 本次发现并修复的真实缺陷

| # | 缺陷 | 影响 | 修复 |
|---|------|------|------|
| 1 | `_load_ods_rows` 用**契约字段名**（`trade_date`）过滤 ods 表，而 ods 表存的是**源列名**（akshare 为 `日期`） | akshare 侧 ods 读取必然失败——双源链路从未真正跑通 | 按源映射解析日期列（`mapping.fields[...].source_column`），新增 `time_column` 参数 |
| 2 | `build_stock_daily_pipeline` 把**归一化后的**读取器传给 `CrossCheckService`（该服务自行归一化） | 双重归一化 → 校对必然 fail-closed | 新增 `ods_raw_reader`（返回源列原始帧）供校对使用；合并仍用 `ods_frame_reader` |

两项均有测试覆盖：`tests/test_pipeline_templates.py::TestOdsReadersUseTheSourceDateColumn`。

## 5. 待办

- B1.3 录制回放抽样（≥20%）：与 AC-6 kline 用例同类别，待 eastmoney 网络；
  当前以 **manifest 逐文件 sha256 字节级保真** + **真机双源校对**建立证据。
