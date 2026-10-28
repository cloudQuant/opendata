# 单条 Fuyao 财报归档调用边界

本说明记录现有 Fuyao 采集桥接和财报 SQL 存储回调的调用方式，不增加新的服务层包装。数据获取由调用方或 fetcher 完成；这里仅处理一个已经获取成功的响应。

## 两阶段调用

1. `opendata.pipeline.fuyao_observation_capture.prepare_fuyao_observation_capture(...)` 校验响应和调用方声明，写入并回读验证原始响应，然后返回 `PreparedFuyaoObservationCapture`。保留其 `.prepared` 字段供 SQL 阶段使用。
2. `opendata.pipeline.financial_observation_store.persist_prepared_financial_observation_capture(connection, prepared=...)` 在调用方持有的财报归档连接和事务中保存已准备的数据。

示例：

```python
capture = prepare_fuyao_observation_capture(
    fetch_result,
    unit_profile=unit_profile,
    provenance=provenance,
    source_attempt_declaration=attempt_declaration,
    writer=raw_writer,
)
prepared = capture.prepared

with archive_engine.connect() as connection:
    # SQLite 要先建立物理事务；BEGIN IMMEDIATE 也会取得写锁。
    connection.exec_driver_sql("BEGIN IMMEDIATE")
    receipt = persist_prepared_financial_observation_capture(
        connection,
        prepared=prepared,
    )
    connection.commit()
```

调用方选择数据库、引擎、写账号、事务边界及提交时机。SQL store 不会建立引擎或自行提交外层事务；它要求同步 SQLAlchemy `Connection` 已处于活动事务内，且 SQLite 的 DBAPI 物理事务已打开。`BEGIN IMMEDIATE` 应在归档连接上、store 回调之前执行。请勿把 REST 查询使用的只读连接交给这个写回调。

## 失败、重试和孤儿原始文件

原始文件写入与 SQL 事务不具备原子性。桥接阶段成功后，即使 store 报错或调用方回滚，原始对象和清单仍会保留。SQL store 在自己的嵌套 savepoint 中写入归档行；写入失败时会回滚该 savepoint，并将数据库异常转换为 `FinancialObservationStoreError`。调用方应保留 `.prepared`，确认外层连接仍可用后，在适当事务中重试同一个 DTO；这样可以复用相同 capture ID，无需重新请求供应商。外层事务回滚只撤销本事务中新写入的 SQL；此前已提交、由幂等重放读取到的数据不会被删除。原始证据也不会随 SQL 回滚。

当前没有持久化的 capture intent、跨进程恢复队列或孤儿清理流程。如果进程在原始文件发布后、调用方持久化恢复状态前崩溃，内存中的 `.prepared` 会丢失。raw artifact 只能证明文件内容和清单可读，不能证明相应 SQL 是否提交，也不足以安全重建 prepared DTO。不得仅凭文件存在或数据库行数猜测提交结果，也不要自动删除这类孤儿证据；恢复责任仍在调用方的持久化任务状态与人工核对流程中。

## 测试范围

对应测试使用临时 SQLite 文件、仓库归档 migration 和合成的类供应商响应。`before_cursor_execute` 故障注入会把中途的 observation INSERT 指向不存在的测试表，触发 SQLite 执行错误。之前已成功执行的 capture 和 observation INSERT 会由 store savepoint 一并回滚；测试检查没有部分写入后，再用同一个 prepared DTO 重试。该用例只验证本地 SQLite 事务行为，不代表 MySQL 锁语义、进程崩溃恢复、真实 Fuyao 报文、供应商回执、生产权限或全历史完整性已经验收。

原始 attempt 声明、unit profile 和 provenance 仍是调用方提供的进程内值。桥接层的字段匹配不能认证 gate、供应商处理结果或来源授权。该切片也不提供定时任务、公共写 API、孤儿回收、PIT 重建或历史完整性证明。
