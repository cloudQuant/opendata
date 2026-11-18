# 迭代3：稳定性、安全与代码质量加固

> 状态：计划编制完成，待评审  
> 日期：2026-11-17  
> 基线：`dev` @ `d7d8dc893`  
> 上游：迭代2（发布仍 NO-GO）

## 文档入口

| 文件 | 用途 |
|---|---|
| [实施计划.md](实施计划.md) | 目标、P0/P1 结论、工作包、里程碑、排期与风险 |
| [验收文档.md](验收文档.md) | AC3-01~AC3-30 的判据、命令、证据与状态定义 |
| [OCR审查发现清单.md](OCR审查发现清单.md) | OCR 三批 368 条原始发现（Critical/High 全文，Medium/Low 摘要） |

## 本轮定位

迭代2 继续扩大 provider 与模型分母之前，先关闭已暴露的运行时正确性、安全边界与并发生命周期问题。本轮**不新增 provider、不扩大 350 模型分母、不做发布验收**；只对现有功能做“可发布前的加固”。

## 审查口径

- OCR：OpenCodeReview CLI v1.13.11，provider `ocr-gateway` / `deepseek-v4.1-flash`
- 范围：尝试扫描 173 文件 / 95,303 行（API/服务/核心/客户端 84、数据层 49、Pipeline 40）
- 覆盖：完整完成 120 文件（61,866 行，64.9%）；53 文件出现 `scan_subtask_error`（A 17 / B 6 / C 30）
- 结果：368 条（Critical 4、High 81、Medium 186、Low 97）；未完成文件的发现可能缺失
- 静态：ruff 14,114 条（主要为 A1 docstring/注解债务）；26 文件待格式化；`a2_check.py` 300s 超时
- 可信度：P0 条目 100% 人工复核；High 已抽查；其余标注“OCR 初筛/待复核”

## P0 清单（已复核）

| ID | 问题 | 位置 |
|---|---|---|
| P0-01 | `.env` 单独设置 `APP_ENV=production` 时默认 SECRET_KEY 被接受 | `opendata/core/config.py:431-443` |
| P0-02 | 随机管理员密码只记录前 3 位，账号永久不可登录 | `opendata/core/database.py:156-177` |
| P0-03 | `INSERT IGNORE` 静默吞错 + 列冲突/截断 + 行哈希歧义 | `opendata/services/data_acquisition.py:475-550` |
| P0-04 | 脚本失败被重试流程记录为 COMPLETED；`rows_processed` 丢失 | `opendata/services/retry_service.py:163-171` |
| P0-05 | refresh token 不注销/不轮换；登出/改密不失效旧令牌 | `opendata/api/auth.py:220-253, 340-365` |
| P0-06 | 下载状态/结果 IDOR；`trigger_download` 未记录 owner | `opendata/api/data.py:88-260` |
| P0-07 | API Key 的 domain scopes 被当作 operation 授权使用 | `opendata/api/published_access.py:318-319` |
| P0-08 | `update_script` 任意赋值 + `module_path` 任意导入 | `opendata/services/script_service.py:112-114, 205` |
| P0-09 | XLSX 导出公式注入（CSV 已转义） | `opendata/api/tables.py:50-52` |
| P0-10 | 管理员写 `.env` 可注入换行；非原子/0644；同步回退泄漏 Engine | `opendata/api/settings.py:41-42, 52, 169` |
| P0-11 | 注册/初始化/Key 旋转 check-then-act 竞态返回 500 | `opendata/api/auth.py:92-94`、`opendata/core/database.py:187-189` |

## 使用方式

1. 先评审 [实施计划.md](实施计划.md) 第 12 节“未决问题”，确认 API Key operation 模型、管理员初始化策略、行哈希迁移策略。
2. 按 M3.1 → M3.2 → M3.3/M3.4 → M3.5 推进，每个工作包独立 PR 到 `dev`。
3. 每条 AC 的证据按 [验收文档.md](验收文档.md) 第 4 节归档到 `docs/evidence/iter3/`。
4. 聚合 `make gate` 未完成隔离环境审查前记为 `NOT_RUN`，不得用静态子项替代。

## 边界声明

- 本目录只包含计划与审查记录，不包含代码修复。
- OCR 输出可能含误报；P0 已复核，其余条目在实施前需按清单逐条确认。
- 本轮通过不代表迭代2的 `SOURCE_VERIFIED`/`DEPLOYMENT_VERIFIED`，也不代表 350 模型完成。
