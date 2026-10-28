# Fuyao 单标的手工组装预检

`opendata.pipeline.fuyao_manual_composition.prepare_fuyao_manual_preflight`
目前只提供默认关闭的配置预检。即使返回 `preflight_only`，也不会创建或启用
`ReviewedJob`、提交 run、启动 worker/scheduler、连接 gate 数据库、reserve attempt
或发送 Fuyao 请求。它不能替代一次手工运行的上线验收。

## 输入与检查

调用方显式传入 `FuyaoManualCompositionConfig`，其中包含现有
`FuyaoProcessDeploymentBinding` 和一个有界 `FuyaoManualQuery`。省略配置或保持
`preflight_enabled=False` 时，函数立即返回 `disabled`，不读取路径，也不检查凭证。

打开预检后，流程会：

1. 重新验证 deployment binding 的路径、bundle/policy SHA-256、grant ID/revision
   形状及其他有界字段。
2. 通过 `FuyaoCredentials` 检查传入 key 非空且可被凭证对象接受。此项不联系 Fuyao，
   不能证明 key 有效、未过期或具备接口权限。
3. 使用现有严格 bundle loader 校验 bundle 文件权限、实际字节 pin 和单标的范围，
   并要求其证券与手工 query 完全一致。
4. 安全读取 source policy 文件并比较独立 SHA-256 pin，解析唯一的 THS 财报查询声明，
   检查其 operator 声明为 `ALLOWED`、无未解析条件、Fuyao host 在允许范围内且声明
   尚未过期。
5. 用现有 manual payload builder 校验 statement、periodicity、日期/limit 和
   idempotency key。

bundle/policy 的 `ALLOWED` 声明及摘要都不证明供应商授权、单位语义或数据真实性。
它们只是对本地配置的结构、字节和 operator 声明进行检查。

## Gate 和执行边界

预检会重新验证 binding 中 grant ID 和 revision 的**格式**，但报告始终将
`gate_grant_status` 设为 `not_checked`。现有 `SourceAttemptGate` 没有 grant 的只读
授权查询接口；对真实请求的当前授权、revision、host seal、quota 与速率限制，仍由
fetcher 在 transport 前通过 gate 原子判定。reserve 会写 attempt tombstone 并可能扣减
quota，因此预检不调用它，也不能提前声称 grant 有效。

当前财报 job factory 返回 `reviewed=False`、`enabled=False`，ReviewedTaskCatalog 和
RunService 会拒绝它。这个模块不会复制或改写 job 标志来绕过拒绝。现阶段没有生产
composition 能把 preflight report 变成可执行 job。启用手工运行需要另一项经过验收的
部署装配工作：明确 review 当前代码版本，构造只允许手工 dispatch 的 reviewed job，
固定 `api` 与 `worker` 角色且不配置 scheduler，并在实际 transport 时保留 gate 的最终
授权检查。

## 凭证

项目现有 `Settings` 支持从本地 `.env` 读取 `FUYAO_API_KEY`。可以由应用配置层读取后
显式构造 `FuyaoProcessDeploymentBinding`；本预检模块本身不加载 `.env`、不查找环境
变量，也不会在报告或异常中包含 key。`.env` 不应提交，测试只能使用合成凭证。

`tests/test_fuyao_manual_composition.py` 的测试代码只写入 `tmp_path` 下的合成 bundle
和 policy 文件，并使用合成 key；测试本身不访问数据库或网络，也不直接读取 `.env`。
普通 pytest 会加载仓库 `conftest.py`，其导入路径可能读取项目 `.env`，所以不能仅凭测试
代码断言一次常规 pytest 完全不接触 `.env`。要保证与项目配置隔离，必须将所需代码和测试
冻结到经过审阅且排除 `.env` 和宿主配置的隔离快照中，从隔离目录运行明确选择的节点，使用
`--noconftest -c /dev/null`；此测试尚未运行，具体快照和命令由集成者审阅批准。
