# 股票 market/symbol 选择合同与启用边界

**状态：内部合同说明，生产查询未启用，尚未验收。** 本文描述当前实现边界，不代表真实来源授权、字段语义、历史完整性或数据库迁移已通过验收。

## 合同形状

`market_symbol_v1` 只在数据合同满足以下条件时成立：

- `filter_fields` 的顺序和内容严格等于 `("market", "symbol")`。
- `market`、`symbol` 字段均声明为非 nullable 字符串，且都属于 `business_key`。
- 合同描述 `time_series` / `business_date`；`time_field` 指向 `date` 类型字段，并且该字段属于 `business_key`。
- 选择值必须成对给出，按原值精确匹配。`market` 最多 32 字符，`symbol` 最多 128 字符；拒绝空值、纯空白、首尾空白、通配符及控制字符。不做大小写转换、交易所推断、证券代码推断、前缀匹配或列表匹配。

该 profile 不固定 `frequency`，也不允许 `market`/`symbol` 与 Sina 的 `instrument_id`/`capture_id` 选择器混用。profile 由 [market_symbol_selection.py](../opendata/data/market_symbol_selection.py) 按合同结构判定；它只验证合同和选择值，不校验数据来源或授权。

合同中的 `filter_fields` 是经校验的数据合同声明。它本身不代表公共 v2 REST 已实现、允许或可查询该过滤条件。

## 当前 REST 状态

公共 v2 的 market/symbol 查询仍关闭。`published_data.py` 中 `_MARKET_SYMBOL_API_ENABLED` 固定为 `False`，不是可由部署配置打开的开关；涉及该 profile 的记录读取和快照操作会按隐藏资源返回 404。数据集元信息可见性不表示记录查询已开放。

datasets 列表和详情仍可能返回合同原始 `filter_fields` 中的 `market`、`symbol`；这只是合同声明的元信息，不代表 records 路由可按这两个字段查询。

当前 records GET 参数及快照创建 POST body 仍遵循旧合同：不包含 `market` 或 `symbol`。GET 中的未知查询参数按请求校验拒绝，POST body 采用 `extra="forbid"`；不要把这些字段传给现有接口，也不要把 `instrument_id`/`capture_id` 改名或拼接为股票选择器。

## 数据和启用要求

- 旧数据中的 NULL `market`/`symbol` 需要逐合同、逐记录审查并制定重建或重新发布方案。不要填空字符串来绕过非空合同：这会改变 payload 和 business key，导致合同、记录摘要/hash 与实际数据不一致。也不要从来源名称、代码前缀或相邻行猜测缺失值。
- MySQL 8 迁移、索引行为、既有 NULL 行处置以及兼容回滚尚未完成生产验收。上线前须在独立、可销毁的 MySQL 8 实例验证迁移与回滚，并由审查人确认历史数据处置。
- 全历史扫描容量和最坏查询计划尚未验收。启用前须针对代表性大 publication 验证索引命中、扫描行数、响应字节数、延迟和分页上限；全表行数或单只股票的短窗口测试不能替代该证据。
- 来源许可、market/symbol 业务口径、上市/退市范围和逐证券全历史完整性仍须各自提供证据。空结果不证明该证券不存在或历史完整。

在这些审查和容量证据通过前，保持 REST 硬关闭，不做生产回填或启用。启用决策应同时更新合同/API 文档、请求/响应 OpenAPI、实际 store 查询与索引、隔离数据库验收和来源审查记录。
