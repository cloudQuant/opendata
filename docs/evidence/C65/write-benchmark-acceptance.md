# C65 主线程写入基准验收

日期：2026-09-30；仅原生3306只读 → 本会话自建33565隔离 MySQL。三档分别使用新进程与空目标表，分页50,000行，实际生产 OdsWriter staging 写入；没有修改原生数据库。

| 档位 | 实写行数 | 写入页 | 耗时秒 | 峰值RSS MiB | 最终状态 |
|---|---:|---:|---:|---:|---|
| 100000 | 100,000 | 2 | 269.011 | 274.91 | limited-complete |
| 1000000 | 1,000,000 | 20 | 424.212 | 348.78 | limited-complete |
| full | 10,310,289 | 207 | 2211.299 | 308.86 | complete |

全量1,031万行的源快照 COUNT、EOF扫描、读数、写数和目标 COUNT 均相等；207页全部完成。10万与100万两档是有限样本，只有全量档证明全源写完。峰值由 `resource.getrusage` 获取，macOS原始单位bytes；日志同时独立采集当前RSS，避免把当前值当峰值。进程启动、源快照COUNT、读取与写入均包含在表中耗时，驱动耗时另见机器记录。100万到1,031万行，行数增加10.31倍而峰值从348.78降到308.86 MiB；三个规模均未呈随总行数线性增加，且远低于2 GiB。此为本机、该表与50,000分页的实测，不承诺任意分页或任何数据域皆同样耗时。

复核入口：`write-benchmark-matrix.json`、三个原始 `.jsonl`、`write-benchmark-matrix-driver.txt`；冻结源码 SHA256 `4a47f4dcc746ad57d5c36ce379aa6816d337aa275e32f3b88eca932ff6107063`。主线程此前独立执行17项边界回归（`benchmark-guard-repair-independent.txt`），源连接被约束为 REPEATABLE READ/READ ONLY，目标限制loopback33565与 `opendata_c65_` 独立schema、空表及完全一致的业务主键/字段。首轮DESCRIBE拒绝已保留失败档案并修复为 INFORMATION_SCHEMA SELECT 后重跑三档。

原始initial/batch日志的status字段沿用了初始error，实际每页仍正常提交；最终status、进程exit、写数和目标COUNT均证明本次完成。此日志状态缺陷已交给执行器单独修复，旧实测原文与冻结源码身份保留。

AC-8|04 与 §5|01 的单源全量写入基准由此完成。此结果尚不证明双源全域落库、十年DataPipeline断点恢复、分区维护或原生部署。
