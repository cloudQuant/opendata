# AFI 204 迁移工具、发布与运行基础

2026-10-26。当前交付是隔离集成分支上的代码与独立数据库验证，尚未迁移生产数据或切换旧调度器。首批6个基础子任务之后，第二批11条开发任务新增持久发布、受控执行、数据集授权和v2快照链路；第三批7条执行任务增加可终止子进程、raw证据、统一入队、主机清单、迁移预演、OpenAPI门禁和股票wire身份校验；第四批补充股票reviewed job、主数据与coverage服务、SQLite持久worker、CLI/角色规划、单任务禁用定义导入和metadata 0007；完整范围仍按 ai-for-investor 的迭代204工作包管理，不能按模块数量推导迁移完成。

## 代码入口

| 模块 | 已实现 | 边界 |
| --- | --- | --- |
| `opendata.migration_inventory` | 只读AST盘点、文件哈希、JSON/CSV、可选任务映射双向检查 | 默认只扫描AFI的 `src/backend/app/data_fetch/scripts`，不是全部生产任务/进程清单 |
| `opendata.pipeline.history_coverage` | 单数据集/来源/标的的日粒度预期与实际日期对账 | 不生成交易日历，不认定证据真实性，不查询仓库或数据源；intraday未支持 |
| `opendata.pipeline.run_identity` | 版本化Job/Run合同、UTC自然槽、幂等键与独立请求快照hash | 不派发任务，不持久化；默认任务disabled |
| `opendata.pipeline.run_ledger` | warehouse 0010、租约、同事务结果提交与终态、数据库UTC时钟模式 | 可信内部回调，不能作为任意Python沙箱；生产worker入口尚未接线 |
| `opendata.pipeline.run_service` / `contract_publication` | 审核的静态handler、身份重放、heartbeat、结果预算、合同校验与原子发布 | rejected不发布；已以真实子进程fixture验证组合，生产worker/systemd尚未接线 |
| `opendata.pipeline.publication_store` | warehouse 0011、追加修订、head/watermark、固定快照、签名cursor | 同dataset增量不能漂移数据合同；窗口仅业务日期，超限显式失败 |
| `opendata.data.platform_contracts` / `dataset_contract_registry` | 字段/单位/时间语义、三元版本身份、有界校验与稳定hash | 服务端显式审核注册；不能从行数推断质量或完整性 |
| `opendata.api.published_access` / `published_data` | 操作scope、数据域、审核grant、v2目录/records/coverage/snapshot | 非日级时间序列、严格PIT和未知参数明确拒绝；不抓上游 |
| `opendata.runtime_inventory` / `migration_catalog` | 真实库只读清单、源码与Provider静态crosswalk | 参数值不公开；主机清单单独采集且保留UNKNOWN；仅有受限SQLite单任务导入原型 |
| `opendata.pipeline.process_executor` | 固定解释器/helper、审核函数注册、JSON IPC、deadline/cancel/checkpoint、进程组回收 | 仅POSIX；不是Python沙箱，不传Engine/RunContext/publisher；全局并发和OS资源配额另配 |
| `opendata.pipeline.raw_evidence` | 流式body SHA、独立capture manifest、私有权限、原子no-clobber、拒绝symlink/FIFO | 崩溃遗留暂存/孤立对象的受控回收与保留策略执行尚未实现 |
| `opendata.pipeline.run_dispatch` | scheduled/manual/backfill统一入队、dry-run、APScheduler真实计划槽回执 | CLI和SQLite worker已有显式SPI联调；生产scheduler启动、管理员API及systemd尚未接线 |
| `opendata.migration_plan` / `host_inventory` | 离线输入hash/目标指纹/审核mapping预演；固定只读命令及有界主机采集 | dry-run不apply；proc关联不等于writer身份，缺权限/截断保留UNKNOWN/PARTIAL |
| `opendata.data.instrument_reference` / `pipeline.coverage_service` | 版本化生命周期、别名、日历证据与publication绑定的coverage | 限期或复用别名须as_of；证据为显式注入，尚无真实主数据持久化或coverage REST接线 |
| `opendata.pipeline.stock_daily_job` | 默认disabled、明确合成响应、原始证据和请求范围校验、标准化 | 不是实际HTTP源worker；source verified仍False |
| `opendata.pipeline.reviewed_worker` | 自有SQLite Engine、真实受控子进程fetch、lease/heartbeat与fenced发布 | 拒绝外部Engine、换库和fork复用；MySQL worker、全局并发和OS硬资源上限尚未实现 |
| `opendata.reviewed_cli` / `pipeline.runtime_roles` | 有界JSON、统一提交、显式worker SPI及角色互斥规划 | 未配置入口exit78；不加载默认DB或改变main legacy启动，实际服务部署另验 |
| `opendata.job_definition_store` / `migration_apply` | 禁用定义幂等保存、冻结计划/hash/mapping及目标指纹检查；metadata 0007 | 原型仅支持调用者保证静止的rollback-journal SQLite和isolated metadata；不迁执行/checkpoint/事实或真实任务 |
| `scripts/quality/check_published_openapi.py` | actual main v1/v2冻结及保守破坏检查，接入make gate | 远端CI尚未执行；不支持的引用/语义明确失败，不能自动更新baseline |
| `opendata_client` | 保留v1，新增独立v2 SnapshotClient、整体deadline/有限重试/分页预算 | v1仍latest-view；v2按快照与行来源序号校验，不含异步导出 |

配套的 ai-for-investor REST 适配器已增加逐行symbol精确校验、严格业务日期、分页回显检查、重复页检查、流式响应及累计字节预算。仍只登记当前股票日线合同，不扩大来源/频率，不将当前采集时间解释成历史PIT时间。

## 静态盘点

在opendata仓库根目录运行，输入仓库只读：

```bash
python scripts/ops/inventory_legacy_scripts.py \
  --source-root /home/yun/Documents/ai-for-investor \
  --format json \
  --output /tmp/afi204-scripts.json
```

输出已存在时默认拒绝覆盖；需要重建报告可显式加 `--overwrite`。扫描脚本树、task-map输入、`.env`命名路径及其符号链接别名始终受保护，不能作为报告输出。语法错误/不可读文件不会静默消失，报告保留错误并以非零状态退出。脚本不被import或执行。

可选 `--task-map /path/to/tasks.json`，形态为：

```json
{"tasks":[{"task_id":"legacy-task-1","script_path":"src/backend/app/data_fetch/scripts/stocks/daily/stock_zh_a_hist.py"}]}
```

这是已脱敏的离线输入，不会自动连接数据库导出任务。路径必须是相对source-root的精确路径；多文件任务可用 `script_paths`。工具会报告无脚本的任务、无映射的入口候选和重复身份。辅助候选保留人工审查，静态匹配不会升级source/task/dataset验证状态。

源码扫描1,066个文件，集合及SHA-256与起始CSV一致，语法/文件系统错误为0；来源验证状态保持 `NOT_VERIFIED`。第二批真实只读数据库确认1,103个任务、1,066条脚本注册，1,056个静态入口均有运行态映射；脱敏摘要见 [afi204-runtime-inventory.json](afi204-runtime-inventory.json)。第三批只读主机报告见 [afi204-host-inventory.json](afi204-host-inventory.json)：20个项目关联unit中12个active；3次show失败使systemd整体UNKNOWN。77个项目路径关联进程仍全部未分类，不能当成77个采集器；checkpoint扫描有界且PARTIAL。动态服务调用和实际writer绑定仍需核对。

## 运行身份与账本

`build_run_identity(TaskDefinition, RunRequest)` 输出：

- `idempotency_key`：运行槽的稳定键。scheduled按job版本、UTC occurrence、分片和显式partition区分；manual/backfill需要显式请求键。
- `payload_hash`：完整业务请求快照的SHA-256。
- `canonical_json`：有界、规范化、可重放的声明式参数快照。

同一运行槽参数变化保持相同key、产生不同快照，`RunLedger.enqueue(key, canonical_json)` 会拒绝冲突。任务版本变化必须显式登记，不能利用参数漂移绕开自然槽幂等。凭据通过独立secret引用注入，不能放入query/partition；验证会拒绝凭据类字段和明显token形态，但不宣称能识别任意伪装字符串。

RunLedger构造器不进行数据库I/O，也不创建表。0010/0011均归 **warehouse Alembic**，账本、发布事实和watermark使用同一个Engine，control metadata独立保留。禁止通过生产启动时的 `create_all` 绕开迁移。

除SQLite外，已在独立MySQL 8.0.46实例验证迁移、并发领取、回滚、修订快照及运行到REST/SDK的组合。生产worker应显式使用 `RunLedger(engine, clock_policy="database")`，租约以同连接数据库UTC判断，拒绝worker覆盖commit clock；默认caller模式仅用于兼容同主机协议和确定性测试。PostgreSQL真实行为仍未验证，APScheduler真实slot到ledger的桥接已在离线测试验证；CLI到SQLite持久worker已通过显式注入组合，实际main启动、生产数据库及systemd接线仍待完成。

## 子进程与统一提交

`ReviewedProcessRegistry` 仅接受显式登记的、可导入的顶层函数。`ReviewedProcessExecutor` 以当前Python的隔离模式启动固定helper，通过有界JSON socket协议传参和收结果；父进程持续检查deadline、取消和checkpoint。业务函数的Python输出被丢弃并计数，原生文件描述符输出进入DEVNULL，异常文本不回传。函数继承环境与OS权限，部署仍需最小权限、资源限制及全局并发控制；这不是运行任意用户策略的安全边界。

`RunDispatcher` 的三种提交模式均调用同一RunService；新任务默认disabled，必须显式审核。APScheduler桥接监听真实 `scheduled_run_times`，只负责入队。`EVENT_JOB_SUBMITTED` 先于executor的misfire判定，入队不能当作执行或发布成功；队列回执保留逐slot错误。Cron采用APScheduler3的五字段语义，weekday只接受 `*` 或 `mon` 至 `sun` 的名字、列表、范围和命名步长；旧数字weekday必须先确认POSIX/Quartz/APScheduler方言并转换，不能静默迁移。

`tests/test_reviewed_run_chain.py` 使用明确的合成进程源，验证真实子进程、raw body/manifest、同事务fenced发布和固定快照，并验证超时、取消和损坏证据不发布。它不证明真实Provider或自然调度周期。

## 原始证据与迁移预演

`RawResponseArtifactWriter` 要求显式私有绝对根目录，目录0700、文件0600。body按SHA寻址，来源/请求摘要hash/观测时间在独立manifest中保留；请求摘要只接受显式字段白名单，不写URL、header、Cookie或原始参数。临时对象与目标以no-follow/原子no-clobber方式处理；SIGKILL/主机崩溃仍可能留下暂存或无manifest对象，生产启用前需补充有锁、带最小年龄的回收和保留策略，不能按文件名直接删除正在写入的对象。

在仓库根目录生成静态crosswalk和离线迁移预演；完整runtime输入应使用受控只读导出的私有报告，公开摘要不能替代：

```bash
python scripts/ops/build_migration_catalog.py \
  --legacy-root /home/yun/Documents/ai-for-investor \
  --opendata-root /home/yun/Documents/opendata-od204 \
  --baseline /path/to/script_migration_inventory.csv \
  --output /path/to/new-static-catalog.json
python scripts/ops/plan_legacy_migration.py \
  --runtime-inventory /path/to/private-runtime-inventory.json \
  --static-catalog /path/to/new-static-catalog.json \
  --output /path/to/new-migration-plan.json
```

输出必须是新文件，不能覆盖输入、代码、配置或其别名。没有逐任务审核mapping、目标白名单与warehouse指纹时，预演保留全部1,103个任务为BLOCKED、0个ready；10条无调度辅助记录单列。静态报告中的877个MISSING_PROVIDER只表示未匹配已注册Fetcher，不能推导877个均需新写适配器；原生wire调用也可能失去旧vendor函数关联。计划不执行SQL、旧脚本、抓源或apply。后续影子补数必须使用隔离目标；每个生产dataset/sink始终只有一个writer。

股票日线已先核对原始JSON的code/market再构建行，避免vendor回填请求symbol造成伪身份校验。当前GovernedHttpClient仍先完整buffer再执行2MiB检查，尚无入口流式硬上限；新的固定参数集也未获成功真实响应。两次公开短窗口请求在HTTP状态前断开，Provider保持verified=False，许可与历史保留边界未知。

## 第四批运行切片

`tests/pipeline/test_reviewed_vertical_flow.py` 将CLI提交和worker-once SPI显式绑定到同一SQLite ledger，经真实Popen传递合成JSON。父进程调用股票任务工厂捕获raw证据、normalize并fenced发布，再通过实际v2路由读取固定publication，最后以独立fixture日历/生命周期计算coverage。坏响应失败后facts、publication head和watermark保持原值。该测试不是源站验证，也没有将coverage服务接到线上coverage路由。

worker只接受 `create_worker_engine` 创建并预热、绑定物理文件和创建PID的Engine；普通外部Engine、内存库、文件替换及fork继承均拒绝。fetch必须是 `ProcessFetchHandler`，可信父端builder/decoder/normalize/publish仍需有界；SQLite busy/pool timeout不等于磁盘或任意SQL的硬截止时间。

`python -m opendata.reviewed_cli` 缺显式应用绑定时返回78，且不读取stdin或默认配置。`ReviewedCLIApplication` 是注入式入口库，`runtime_roles`只验证角色规划；它们尚未替换main lifespan和legacy scheduler。CLI的worker SPI与持久worker的无参数run_once通过明确adapter连接，部署方不得只写角色配置便假定旧采集器已经停止。

公共RunRequest在身份序列化/ledger之前拒绝连接配置字段、嵌套凭据、JDBC/ODBC、URI userinfo和敏感query；已识别URI解析失败统一拒绝。验证错误隐藏输入并使用固定键路径，model_construct输入也须重验证。普通HTTPS和业务文本可用；这不替代secret引用注入或最小权限。

metadata `0007` 正确继承0006，提供与store相同的冻结DDL。隔离SQLite验证upgrade/downgrade/upgrade、相邻表保留及store互通；MySQL仅离线DDL编译，不宣称新迁移已在真实MySQL运行。当前importer仍只接受专用isolated metadata，不能直接用于包含legacy控制表的现有metadata库。

目标fingerprint检查只支持稳定、无sidecar的rollback-journal SQLite，使用新建只读连接避免借用旧pool；调用者必须保证整个检查期间数据库静止并串行化写入。`nolock=1`本身不提供锁或并发一致性，参见 [SQLite URI约束](https://www.sqlite.org/uri.html)。生产MySQL目标、备份恢复凭证、执行/checkpoint迁移和真实task apply仍未完成，完整1,103项计划保持BLOCKED。

## v2 运行配置与边界

新路由在实际应用挂载为 `/api/v2`，保留v1兼容入口；机器契约见 [afi204-v2-openapi.json](afi204-v2-openapi.json)。服务端必须显式配置warehouse身份、`PUBLISHED_DATASET_CONTRACTS_FILE`、`PUBLISHED_DATASET_GRANTS_FILE` 和至少32字节的 `PUBLISHED_DATA_CURSOR_SIGNING_KEY`。缺配置、缺迁移、合同hash不匹配或权限不足均fail-closed，不自动建表或回落到上游数据源。

v2当前提供6条路径：datasets列表/详情/records/coverage，以及snapshot创建/读取。支持有完整半开日期窗口的业务日期时间序列；不支持intraday/event窗口、严格PIT、通用筛选/投影或导出。coverage没有独立证据时返回UNKNOWN。创建snapshot的POST不提供幂等键合同，因此SDK不自动重试该POST；每次分页重新验证权限。

每行保留origin publication ID/sequence；它可以早于快照边界。客户端检查sequence不超出固定快照，同序号时ID精确一致。不能将所有行重标成最新publication来隐藏继承血缘。同步快照上限100,000条、page读取预算8MiB，超限报错，不截断为“完整”。

## 独立数据库验收

`tests/test_afi204_owned_mysql.py` 为每个案例新建随机 `od204_test_*` 库并在finally删除，普通测试默认skip。仅对测试MySQL实例显式注入 `OD204_MYSQL_ADMIN_URL` 后运行：

```bash
APP_ENV=testing TESTING=true REDIS_URL='' PYTHONPATH="$PWD:$PWD/opendata_client" \
python -m pytest -o addopts='' tests/test_afi204_owned_mysql.py -q
```

本次本地实例使用独立临时datadir、Unix socket和skip-networking；七项真实MySQL案例通过。全链采集输入是明确标注的合成数据，HTTP身份为测试主体；不证明真实供应商、生产凭据或自然调度周期。仓库门禁仍受既有历史证据文件缺失阻断，不能以定向测试替代完整gate。

## 定向复算命令

在已安装项目依赖的独立环境中、仓库根目录执行。以下是可单独复算的组合边界；测试输入不接生产数据源：

```bash
APP_ENV=testing TESTING=true REDIS_URL='' PYTHONPATH="$PWD:$PWD/opendata_client" \
python -m pytest -o addopts='' -q \
  tests/test_platform_contracts.py tests/test_dataset_contract_registry.py \
  tests/test_run_identity.py tests/test_run_identity_ledger_integration.py \
  tests/test_run_ledger.py tests/test_run_ledger_atomic.py \
  tests/test_run_ledger_database_clock.py tests/test_run_service.py \
  tests/test_contract_publication.py tests/test_publication_store.py \
  tests/test_published_access.py tests/test_published_data_api.py \
  tests/test_snapshot_client.py
```

完整的本轮组合输出、覆盖模块分母和独立安装收据hash记录在验证JSON中；该命令用于复核核心链，不宣称与1120项组合的原始文件选择完全相同。`make brand-check zero-dep-check public-api-quality` 是三项独立门禁，不能代表 `make gate`。

## 日粒度历史覆盖

调用者提供dataset/source/series、请求范围、独立起点证据、带身份/版本/覆盖范围的可信日历以及实际观察日期。评估器输出head/interior/tail、重复/意外日期及覆盖状态。

日历范围未知或截断、独立起点未知时不能得到完整历史认证。源保留边界只限制尚缺失的日期，已经归档的早期真实记录仍可计入覆盖。snapshot-only必须有匹配的归档范围与证据，单日快照也不能自动证明历史。输出的COMPLETE只说明输入证据支持的日粒度键覆盖，不证明字段值正确、许可有效或源端真实完整；这些需要独立DQ和真实来源验证。

## 下一波接线顺序

1. 为实际股票任务接入Provider、原始证据、日历和coverage，验证来源与字段语义。
2. 将已有SQLite worker、CLI/APScheduler SPI与角色规划接到真实受审配置，补生产数据库及systemd启动边界。
3. 按crosswalk分模型族适配剩余脚本，补旧task到job/dataset/sink映射和存量导入dry-run。
4. 扩展其他资产、时间语义、筛选和导出，再迁移剩余业务消费者和管理界面。
5. 完成逐域对账、自然周期和回滚演练后，再实施生产单写者切换。

没有修改旧业务库、运行中的scheduler、systemd、源Key或生产API开关；新DDL只在测试拥有的数据库验证。离线通过不能替代完整 `make gate`、真实源及部署验收。

首批历史收据见 [afi204-offline-validation.json](afi204-offline-validation.json)，第二批见 [afi204-wave2-validation.json](afi204-wave2-validation.json)，第三批见 [afi204-wave3-validation.json](afi204-wave3-validation.json)。第三批具体测试选择、环境和覆盖分母由其命令收据保留；均不能替代生产切换验收。

本轮22份测试日志、覆盖数据、安装wheel和脱敏库存已按hash复制到原OpenData仓库的 `var/reports/afi204/2026-10-26-wave2`（Git忽略，目录0700/文件0600）；归档索引见 [afi204-wave2-archive.json](afi204-wave2-archive.json)。这是同主机的持久目录，不是异机备份或恢复演练。历史门禁缺失5130个支撑引用的只读审计中，有4024个候选副本可匹配本地Git blob的精确SHA；仍有1031个候选、73份收据及分类/审批原件无匹配，未恢复或改pin。

第三批收据索引见 [afi204-wave3-archive.json](afi204-wave3-archive.json)。第三批当时的完整make gate在secret-check停止：固定Gitleaks8.30.1缺失，实际扫描提交数为0、发现数N/A；后续gate子项未因短路而获得通过状态。OpenAPI兼容门禁已接入现有CI调用的make gate，但本地未push或运行远端CI。

第四批最终组合、当前source commit、受检模块和整仓gate状态见 [afi204-wave4-validation.json](afi204-wave4-validation.json)，收据索引见 [afi204-wave4-archive.json](afi204-wave4-archive.json)。固定Gitleaks工具在本批已恢复；第三批工具缺失的历史结果不作改写。
