# C3 旧表转移 — 验收证据索引（AC-15）

> 迭代 1C / 里程碑 C3
> 日期：2026-09-24
> 工具：`scripts/codemod/legacy_transfer.py`（--assess / --migrate / --verify / --drop）

## 1. 转移评估（--assess）

| 项目 | 结果 |
|------|------|
| 旧仓库（`akshare_data`）表数 | **1026** |
| 其中 P0 域有迁移映射（`MAPPINGS`） | 1（`STOCK_ZH_A_HIST`） |
| 其余 1025 表 | 无 P0 ods 映射 → **保持只读**，待其域搬运（P1/P2）后按同一工具扩展映射 |

评估报告：`assess.json`（本目录，逐表行数 + 是否可迁移）。

## 2. 迁移（--migrate）

| 项目 | 结果 |
|------|------|
| 源表 | `akshare_data.STOCK_ZH_A_HIST`（A 股日线历史，2026-01-05..2026-07-21） |
| 目标 | `ods_stock_daily_akshare`（此前为空——akshare 通道受 eastmoney 网络阻塞） |
| 语义 | `INSERT IGNORE`（key 冲突以新抓为准，旧数据只补缺口，AC-15） |
| 行数 | **617,371 → 617,371 全部落库**（`_source='akshare'`，批量 `_batch_id` 留痕） |
| 日期转换 | `STR_TO_DATE`（`CAST(NULLIF(...) AS DATE)` 在该服务器 collation 下对合法 ISO 日期返回 NULL，服务器 quirk，已规避并注释） |

迁移记录：`migrate-full.json`（本目录）。

## 3. 验证（--verify）

| 项目 | 结果 |
|------|------|
| 旧表行数 | 617,371 |
| ods 迁移行数 | 617,371 |
| 抽样键指纹（前 1000 distinct (股票代码, 日期)，MD5） | **一致**（`sample_matches: true`） |

验证记录：`verify-full.json`（本目录）。

## 4. DROP 状态

`STOCK_ZH_A_HIST` **暂不 DROP**：工具支持 `--drop --yes`，但 617k 行属真实存量，
且 ods 侧已完整承接。DROP 属破坏性操作，留待用户在验收回顾中明确拍板
（工具与流程已就绪：验证通过后 `--drop --yes` 即完成 AC-15 闭环）。

## 5. 复算

```
set -a; . ./.env; set +a
python scripts/codemod/legacy_transfer.py --assess --out docs/evidence/C3/assess.json
python scripts/codemod/legacy_transfer.py --migrate STOCK_ZH_A_HIST --out docs/evidence/C3/migrate-full.json
python scripts/codemod/legacy_transfer.py --verify STOCK_ZH_A_HIST --out docs/evidence/C3/verify-full.json
```
