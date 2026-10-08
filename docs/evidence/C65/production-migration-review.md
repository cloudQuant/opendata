# C65 正式数仓结构升级执行材料

日期：2026-10-01。主审：Codex 主智能体。当前基线 HEAD：`d552c081950edffce167e3dfc0be74f37a6950a1`。状态：离线 SQL 导出 PASS；正式执行 NOT_RUN；生产3306写入0。

`production-migration-offline.txt` 留存 Anaconda 外层与固定环境的实际命令、完整 SQL、日期和 exit。Alembic `--sql` 走 `run_migrations_offline()`，没有创建数据库连接。本次明确范围为数仓 `0005_dwd_stock_adjust → 0007_affine_stock_adjust`。

SQL 创建 `dwd_index_daily`、`dwd_futures_daily`、`dwd_option_daily` 三张契约日线表，并向 `dwd_stock_adjust` 添加 nullable 的 `qfq_scale`、`qfq_offset`、`hfq_scale`、`hfq_offset`、`adjustment_version`、`legacy_source` 六列；另有两条 Alembic 版本推进。未包含 DROP、TRUNCATE 或 DELETE。

正式执行前须复核目标确为正式数仓、版本仍为0005、六列尚不存在，并保留成功备份/恢复材料。生产 ALTER 的实际锁等待、耗时与恢复窗口未测量；不能由隔离迁移通过推定。执行后还须核对完整注册分区表、跨年写入、精确分区 count 与复跑维护结果；这份结构升级 SQL 本身不闭合 AC-8|05/06。

本文不授权正式库写入，也不包含旧库删除。旧库1026表仍按用户决定保留，待逐表迁移审查后再决定删除。
