# 每日 provider 巡检

`p0-provider-patrol` 是后端内置的每日调度任务，cron 暂设为 `18:00 UTC`。这是占位时点，
需要按真实业务更新时间校准。任务只有在 `ENABLE_SCHEDULER=true` 且
`PATROL_ENABLED=true` 时才会请求 provider；`PATROL_ENABLED` 默认是 `false`，此时任务只返回
`disabled` 报告，不访问上游。

## 巡检范围

P0 权威范围取自[需求文档 §6.2](迭代计划/迭代1-重构数据中台/需求文档.md#62-数据域优先级)：
`stock_daily`、`stock_adjust`、`stock_action`、`financial_statement`、
`financial_indicator`、`index_constituent`、`futures_daily`、`futures_fundamentals`。
`domains.yaml` 中只有显式 `priority: P0` 的域参与选择。未标优先级的域是 unknown，不能推断为
P2。域还必须有已验证、可路由的 capability 和已配置巡检查询；无 provider、未验证或缺查询的腿
会列为 unknown / ineligible，不会被当成成功，也不会清除失败计数。

早期 `tests/test_p0_providers.py` 中的 `P0_DOMAINS` 5 域集合是历史测试子集，不是当前 P0
权威范围。权威范围以需求 §6.2 和 `domains.yaml` 显式优先级为准。

## 失败与告警

连续失败计数和待发送通知默认保存在 `DATA_DIR/provider_patrol_failures.json`，同目录的 `.lock`
文件用来串行化同主机进程写入。状态只保存域、来源、失败分类、次数、UTC 时间和有界的安全通知
outbox；不保存 provider 错误正文、响应或凭证。确保 `DATA_DIR` 是同一主机上所有调度进程共享且
可写的持久目录，并在备份中保留该状态文件。JSON 损坏或锁不可用时任务失败关闭，不会重置计数。

`PATROL_FAILURE_ALERT_THRESHOLD` 默认 `2`。达到阈值时任务通过现有 WebSocket
`task_notification` 通道广播一条安全事件；相同失败 streak 不会重复告警。对应 capability 的
后续成功会清除它自己的计数并广播恢复事件。probe gap、未验证腿、缺失结果和未知分类不会计作
成功或失败。Key 观察仍写入现有近期观察 store，交给独立的被动 Key 健康任务处理；本巡检不发送
SMTP 邮件。

通知先与失败计数一起原子写入 outbox，只有 WebSocket sink 成功后才确认移除；失败会在后续调度运行
重试。若进程在 sink 成功、确认落盘之前崩溃，或多个调度进程并发处理同一待发送项，同一事件可能重复
投递，因此交付语义是至少一次，消费端应按 `notification_id` 去重，不能视为恰好一次。

期货滚动探针可能解析期货月份；如果将来 P0 查询依赖远程目录读取，报告必须列出这类 provider
依赖请求。当前每日 P0 集合不包括 option 目录探针。巡检只按 `domains.yaml` 选择目标，不改变
provider 注册或自动路由优先级。
