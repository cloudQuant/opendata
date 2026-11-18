# 迭代3 审查证据

> 基线：`dev` @ `d7d8dc893`  
> 日期：2026-11-17  
> 环境：Linux / Python 3.10+ / OpenCodeReview CLI v1.13.11

## 1. 工具与模型

- OCR CLI：`@alibaba-group/open-code-review` v1.13.11（binary linux/amd64）
- Provider：`ocr-gateway`（Anthropic 协议兼容端点，凭据来自本机环境变量；本目录不保存任何凭据）
- Model：`deepseek-v4.1-flash`
- 连接自检：`ocr llm test` → `✓ Connection test successful` / `✓ Tool-call round trip verified`

## 2. 扫描范围

| 批次 | 路径 | 文件数 | 行数 | 评论数 | 状态 |
|---|---|---:|---:|---:|---|
| A | `opendata/api`、`opendata/core`、`opendata/services`、`opendata/models`、`opendata/utils`、`opendata_client` | 84 | 31,692 | 234 | completed_with_errors |
| B | `opendata/data` 顶层 + `opendata/data/providers/_engine` | 49 | 30,396 | 100 | completed_with_errors |
| C | `opendata/pipeline` 关键 40 文件 | 40 | 33,215 | 34 | completed_with_errors |

合计：173 文件 / 95,303 行 / 368 条评论（Critical 4、High 81、Medium 186、Low 97）。

## 3. 命令

```bash
# 预览
ocr scan --preview --path "$PATHS" --exclude '**/_vendor/**,**/__pycache__/**'

# 执行（示例，批次 A）
ocr scan --audience agent --format json --output /tmp/ocr_A.json \
  --concurrency 6 --timeout 20 \
  --path "$PATHS" --exclude '**/_vendor/**,**/__pycache__/**' \
  --background "..."

# 连接自检
ocr llm test
```

原始 JSON 已压缩归档为 `ocr/ocr_A.json.gz`、`ocr/ocr_B.json.gz`、`ocr/ocr_C.json.gz`；校验和见 `ocr/SHA256SUMS.txt`。

## 4. 静态基线

```bash
ruff check opendata scripts --statistics
ruff format --check opendata scripts
python scripts/quality/a2_check.py
```

- ruff：14,114 条（主要为 A1 docstring/注解债务；高信号项见计划 §3.3）。
- ruff format：26 文件待格式化（需排除 `_vendor` 与生成脚本后处理）。
- `a2_check.py`：300s 超时，未取得完整读数；不得以缩小扫描面解决。

## 5. 人工复核记录

已在计划正文标注“已复核”的 P0 条目，复核方式包括：

- `opendata/core/config.py`：用临时 `.env` 文件复现 `APP_ENV=production` 仅写文件时默认密钥被接受。
- `opendata/services/data_acquisition.py`、`data_service.py`、`retry_service.py`、`api/auth.py`、`api/data.py`、`published_access.py`、`script_service.py`、`api/tables.py`、`api/settings.py`、`core/database.py`：逐段阅读代码路径确认。

## 6. 边界

- 未运行数据库集成测试、未启动生产配置、未做真实来源对照。
- OCR 状态 `completed_with_errors` 表示扫描完成但存在文件级错误；不得据此声称所有文件均无遗漏。
- 本目录只保存审查证据，不表示任何缺陷已修复，也不表示迭代2发布验收通过。


## 7. 覆盖缺口（2026-11-17 复核）

OCR 的 `summary.files_reviewed` 统计的是“尝试扫描的文件数”，不是“子任务完整完成的文件数”。三批 `warnings` 实际记录了 53 个唯一文件出现 `scan_subtask_error`（上下文压缩超限、LLM 400、请求超时或工具轮次耗尽）：

- 批次 A：17 个文件；
- 批次 B：6 个文件；
- 批次 C：30 个文件（Pipeline 关键文件受影响最严重）。

按文件计：初扫完整完成 120 / 173（69.4%）；按行计：61,866 / 95,303（64.9%）。

2026-11-17 对 6 个高风险数据层失败文件做了小批次重扫（`/tmp/ocr_retry_data.json`，concurrency=1，timeout=40，no-summary/no-plan）：完成 3 个（`request_summary.py`、`financial_observation.py`、`providers/_engine/row_expansion.py`），仍有 3 个失败（`adjust.py`、`raw_response_cache.py`、`source_policy.py`）。新增 8 条（medium 2、low 6）。

重扫后：完整完成 **123 / 173（71.1%）**；按行 **62,989 / 95,303（66.1%）**；剩余 50 个失败文件。

因此：

1. 本目录与 `OCR审查发现清单.md` 只能证明“已完成的 120 个文件”的发现，不能声称全仓 173 文件均已审查。
2. 失败文件中的发现可能缺失，尤其是 `publication_store.py`、`process_executor.py`、`run_ledger.py`、`jobs.py`、`ods_writer.py`、`partitions.py`、`raw_response_cache.py`、`source_policy.py` 等高风险文件。
3. 计划 WP3-07 已将“补齐失败文件审查”列为工作项；在补齐前，相关结论一律标记为“未覆盖/待复核”。
