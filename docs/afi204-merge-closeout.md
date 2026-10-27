# 迭代 204 已验收代码合并记录

日期：2026-10-27。维护者要求本次仅合并已有验收通过证据的开发代码，允许作者自审，暂不处理 CI 故障；后续以主目录 `dev` 为开发基线，减少工作区。

| 项目 | PR | 合入 dev 的提交 | 验收范围 |
| --- | --- | --- | --- |
| OpenData | [#1](https://github.com/cloudQuant/opendata/pull/1) | `d3ce648eb88e3feb4c0608db35d7188fc8d8c35c` | W1–W11 数据中台基础能力；W11 核心 469 passed、1 个明确排除、0 warnings，OpenAPI 24 passed，相关静态检查通过 |
| ai-for-investor | [#45](https://github.com/cloudQuant/ai-for-investor/pull/45) | `b0fc74be9efcd5ea104a35734018b59a72845770` | REST 消费和股票日线图表；前端 186 tests、lint、build、typecheck 通过，后端源码与此前 203 项专项验收版本一致 |

OpenData PR 固定在 `aee076c83c1067a35da009fa2e8c061445401f72`，与 W11 运行锚点 `fbec7066f9ea58e2e4dcdcf445a4af46b37a8b1e` 之间仅增加公开验收及归档文档，详见 [W11 验收记录](afi204-wave11-validation.json)。AFI 前端树与已验收的 `343ee7707ce79e816714cd0a20f9acd41f8ebee4` 一致；后端源码自 `6b8f13c6d6c7d8cec360ebed63ae7098288a5a2c` 后未改变。两份作者自审 COMMENT review 均绑定各自 PR head。

以下内容未合入：OpenData W11 之后的 12 个 W12 新浪进程采集相关提交、未验收的 CI 修复（`alembic/env.py`、`opendata/alembic_metadata.py`、`tests/test_alembic_target_metadata.py`），以及临时工作区中未提交的迁移目录文档。清理工作区前须保存 Git 提交、未提交补丁、必要文件和验收记录；归档副本不能作为验收通过的证据。

远端 CI 的失败或跳过保留，不声明全量测试、完整 `make gate` 或生产验收通过。本次没有启用生产采集、调度或数据库迁移。全 A 股范围已确定；权威标的全集、北交所能力、历史完整性、来源权限、运维及生产切换仍需后续工作。
