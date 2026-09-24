# C2 上游同步收口 — 验收证据索引

> 迭代 1C / 里程碑 C2（对应验收文档 AC-12 收口）
> 日期：2026-09-24
> 复算：`python scripts/codemod/fetch_upstream.py --diff --upstream <akshare clone> [--ref <commit>]`

## 1. 交付物

| # | 任务 | 结果 |
|---|------|------|
| C2-1 | 上游同步工具 | `scripts/codemod/fetch_upstream.py --diff`：对锁定 commit 与目标 ref 产出**新增/修改/删除清单** + **关键函数签名差异**（ast 解析）+ **锁定 sha256 校验**；10 个单测（`tests/test_fetch_upstream.py`，双 commit 迷你仓库） |
| C2-2 | 维护预算机制 | 实施计划 §2.6 每迭代预留 10~15% 人力；「失效即弃」流程见下 |
| C2-3 | 演练记录 | `sync-drill-report.md`（本目录）：以参考仓库历史 commit `fcdbf25` 模拟上游新版本，产出 **147 文件变更清单** + 函数签名差异 + 锁校验通过 |

## 2. 演练报告要点（sync-drill-report.md）

- 变更清单：147 个文件（修改为主，无删除）
- 函数签名差异示例：`air/air_hebei.py` 移除 `_empty_air_quality_hebei()` 等
- **锁校验：锁定文件 sha256 与冻结提交 `c4f6a63` 完全一致**（基线可复现）
- 复算：`python scripts/codemod/fetch_upstream.py --diff --upstream /Users/yunjinqi/Documents/new_projects/akshare --ref fcdbf25 --out docs/evidence/C2/sync-drill-report.md`

## 3. 失效即弃流程（落档）

1. `fetch_upstream.py --diff` 产出变更清单；
2. 评估：签名变更/新增端点/停服影响（对照表 `docs/proposals/openbb-migration/` 口径）；
3. `scripts/codemod/port_module.py` 按新 commit 重跑（跳过 `manual_edits`）；
4. 保真对照（`compare_with_upstream.py`）→ 提交并 bump `upstream.lock`；
5. 上游停服/失效接口按注册表健康标记降级，**不承诺永久可用**（实施计划 §8.4，「失效即弃」）。

## 4. 门禁

`make gate` 全绿（含 `tests/test_fetch_upstream.py` 10 项）。
