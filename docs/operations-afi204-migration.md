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
| `opendata.data.instrument_reference` / `pipeline.coverage_service` / `data.coverage_evidence_registry` | 版本化生命周期、日历证据、精确publication pin与coverage REST接线 | 限期或复用别名须as_of；证据由部署侧显式注入，目前仅允许四个单标的THS指数数据集；没有真实证据时维持UNKNOWN |
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

### Runtime inventory 的 MySQL 身份与快照前置条件

`scripts/ops/inventory_legacy_runtime.py` 的 CLI 每次都必须提供 operator 审核的身份 pin 文件，例如 `--identity-file /secure/local/afi204-database-identities.json`；该 CLI 只连接 MySQL。直接调用 `build_runtime_inventory` 时，MySQL 目标也必须提供 identity pin；仅隔离测试可显式开启 SQLite API 模式。文件使用 schema version `1`，包含 `metadata` 与 `warehouse` 两项；每项仅包含固定数据库名（`ai_for_investor` / `akshare_data`）和规范小写 `server_uuid`。它与含数据库 URL 的 `.env` 分开保存。pin 必须从可独立信任的实例记录核验后填写；首次盘点观察到的 UUID 不能自动成为信任基准。pin 文件检查依赖 POSIX 的 `O_NOFOLLOW` 和 effective UID 接口；若平台不支持则失败关闭。`O_NOFOLLOW` 只保护最终路径组件，中间目录组件里的符号链接仍会被跟随。程序要求最终文件是普通文件、由 root 或当前用户所有、不可被 group/other 写入，且没有执行或特殊权限位；它不解析 ACL，也不验证目录链。操作员必须保护所有父目录及其 ACL，避免目录替换、硬链接滥用和同 UID 进程篡改；root、文件所有者及能改写父目录的主体都属于运维信任边界。pin 文件没有数字签名，SHA-256 只记录本次读取字节的内容摘要，不提供来源认证或信任证明。盘点会在只读事务内、读取业务表前确认当前 `DATABASE()` 和 `@@server_uuid` 与 pin 相符，并在本地报告中记录实际实例信息及 pin 文件 SHA-256。UUID 是实例标识，不是认证凭据；报告仍含运行清单明细，应留在受限的本地目录。

运行时还必须选择已核实为服务实际配置来源的 URL 文件或显式 URL，例如：

```bash
python scripts/ops/inventory_legacy_runtime.py \
  --env-file /secure/local/verified-service-database.env \
  --identity-file /secure/local/afi204-database-identities.json \
  --source-root /home/yun/Documents/ai-for-investor \
  --output /secure/local/afi204-runtime-inventory.json
```

示例路径不是默认配置；在运行前必须独立确认 URL 文件与服务实际连接配置一致。工具对 `.env` 只读取最多 1 MiB 的一份 UTF-8 内容，用这一份受控读取同时解析两个 URL 并计算报告中的 `env_file_sha256`；允许其他合法 dotenv 键，但不会使用或输出它们。两个目标 URL 键各自只能定义一次；重复目标键或任何格式错误的行都会失败关闭。解析不执行变量插值。显式 URL 模式没有文件摘要。该 SHA 只绑定报告到解析过的字节，不证明文件路径、来源或 URL 与运行服务配置相同，也不是签名。身份文件只绑定身份，不应放入 DSN、密码或其他凭据。

读取调度记录前，`ak_scheduled_tasks` 和 `ak_data_scripts` 必须都恰好对应一张 `BASE TABLE` 且使用 InnoDB。任一缺失、视图、重复元数据或非 InnoDB 引擎都会终止盘点，不读取这两张表的行。成功的 MySQL 报告在顶层 `runtime_table_engine_check` 记录 `status: VERIFIED`、检查名称、`information_schema.TABLES` 来源、metadata 数据库名，以及两表观察到的 `table_type` 和 `engine`；只有两次 metadata 查询均成功并通过检查后才会产生此证据。SQLite 报告将该字段置为 `null`，不会声称做过 InnoDB 检查。InnoDB + 同一 MySQL `REPEATABLE READ` 一致性只读事务为两张表的行读取提供同库快照。盘点期间必须冻结 metadata 与 warehouse 两个库中所有相关对象的 DDL：不仅是这两张调度表，也包括 warehouse 的表、列和索引。warehouse schema 通过多次独立的 `information_schema` 查询收集，metadata 的表定义也分别查询；事务不能保证这些数据字典查询构成原子目录快照，DDL 并发可能让报告混合不同结构时点。即使 DDL 冻结，metadata 与 warehouse 仍使用不同连接和事务，不构成跨库原子快照。SQLite 仅供显式隔离测试使用，不需要 MySQL UUID 或 InnoDB 检查。

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

`tests/pipeline/test_reviewed_vertical_flow.py` 将CLI提交和worker-once SPI显式绑定到同一SQLite ledger，经真实Popen传递合成JSON。父进程调用股票任务工厂捕获raw证据、normalize并fenced发布，再通过实际v2路由读取固定publication，最后以独立fixture日历/生命周期计算coverage。坏响应失败后facts、publication head和watermark保持原值。该测试不是源站验证；coverage REST的publication pin、symbol边界和超限拒绝另由使用合成发布与fixture evidence的隔离API测试覆盖，不证明真实源数据完整。

worker只接受 `create_worker_engine` 创建并预热、绑定物理文件和创建PID的Engine；普通外部Engine、内存库、文件替换及fork继承均拒绝。fetch必须是 `ProcessFetchHandler`，可信父端builder/decoder/normalize/publish仍需有界；SQLite busy/pool timeout不等于磁盘或任意SQL的硬截止时间。

`python -m opendata.reviewed_cli` 缺显式应用绑定时返回78，且不读取stdin或默认配置。`ReviewedCLIApplication` 是注入式入口库，`runtime_roles`只验证角色规划；它们尚未替换main lifespan和legacy scheduler。CLI的worker SPI与持久worker的无参数run_once通过明确adapter连接，部署方不得只写角色配置便假定旧采集器已经停止。

公共RunRequest在身份序列化/ledger之前拒绝连接配置字段、嵌套凭据、JDBC/ODBC、URI userinfo和敏感query；已识别URI解析失败统一拒绝。验证错误隐藏输入并使用固定键路径，model_construct输入也须重验证。普通HTTPS和业务文本可用；这不替代secret引用注入或最小权限。

metadata `0007` 正确继承0006，提供与store相同的冻结DDL。隔离SQLite验证upgrade/downgrade/upgrade、相邻表保留及store互通；MySQL仅离线DDL编译，不宣称新迁移已在真实MySQL运行。当前importer仍只接受专用isolated metadata，不能直接用于包含legacy控制表的现有metadata库。

目标fingerprint检查只支持稳定、无sidecar的rollback-journal SQLite，使用新建只读连接避免借用旧pool；调用者必须保证整个检查期间数据库静止并串行化写入。`nolock=1`本身不提供锁或并发一致性，参见 [SQLite URI约束](https://www.sqlite.org/uri.html)。生产MySQL目标、备份恢复凭证、执行/checkpoint迁移和真实task apply仍未完成，完整1,103项计划保持BLOCKED。

## v2 运行配置与边界

新路由在实际应用挂载为 `/api/v2`，保留v1兼容入口；机器契约见 [afi204-v2-openapi.json](afi204-v2-openapi.json)。服务端必须显式配置warehouse身份、`PUBLISHED_DATASET_CONTRACTS_FILE`、`PUBLISHED_DATASET_GRANTS_FILE` 和至少32字节的 `PUBLISHED_DATA_CURSOR_SIGNING_KEY`。`PUBLISHED_DATASET_COVERAGE_EVIDENCE_FILE` 是可选的operator-owned JSON路径；未配置或entries为空时coverage保持UNKNOWN。缺其他必需配置、缺迁移、合同hash不匹配或权限不足均fail-closed，不自动建表或回落到上游数据源。

v2当前提供6条路径：datasets列表/详情/records/coverage，以及snapshot创建/读取。支持有完整半开日期窗口的业务日期时间序列；不支持intraday/event窗口、严格PIT、通用筛选/投影或导出。coverage可用`publication_id`选择固定publication；缺少与精确dataset/source/publication三元组匹配的独立证据时返回UNKNOWN。coverage证据文件仅接受四个单标的THS指数数据集，并绑定唯一生命周期、日历、publication scan范围，以及schema 2的先验`visible_record_set` pin。API会在单一一致性快照中读取publication与该序号下完整可见集合，重新归一化每条fact并核验持久`record_hash`，再按`visible-record-set-v1`重算完整集合摘要；只有算法、摘要、行数、publication序号及合同SHA-256全部和事先注入的operator pin相同，才继续coverage评估。单标的链要求每条可见fact的payload `symbol`一致；超出100,000条或64MiB、不能保证一致快照、记录hash损坏或pin不匹配时均fail-closed，不截断，也不返回COMPLETE。旧schema 1可以读取以兼容配置迁移，但缺完整集合pin时恒为UNKNOWN。响应带有`operator-attestation-sha256`和记录集pin引用；持有原配置时摘要可识别/校验当次语义内容是否一致，但不能从摘要重建配置。该hash是内容指纹，不是签名或真实性证明。

schema 2中的`visible_record_set.sha256`必须在目标publication被评估之前，由受控的独立发布/验收流程从可信的发布输入或独立核验的归档中产生并审核；不能把API首次扫描待验数据库得到的digest复制进sidecar来自我认证。API运行身份只读该sidecar，配置写入权与数据库写权限应分离，并保留按digest可查的已审批配置版本或审计记录。`visible-record-set-v1`摘要使用canonical JSON UTF-8、每个JSON对象后附换行；header绑定dataset/source/publication ID及sequence、schema/semantic/unit/adjustment、合同hash、算法版本、排序版本和可见行数；记录按business_date NULLS LAST、日期、UTF-8 business_key排序，每行绑定business_key、revision、business_date、来源publication ID/sequence和使用`normalize-records-canonical-v1`重算的record hash。摘要用于检测已批准pin与数据库可见集合不一致；它本身不证明operator审批、源端真实完整，也无法防止同一恶意方同时修改数据库、sidecar和程序。当前历史断言使用Asia/Shanghai业务日：`status_as_of`、请求/扫描起点和实际观察日期不得晚于今日；半开请求/扫描终点最多到明日（以覆盖今日），超前边界返回UNKNOWN。日历可定义未来交易日，但不能据此把未来日期当作已观察历史。生产启用COMPLETE仍需真实独立来源材料、配置审批/权限隔离及恢复审计证据。代码只检查配置结构和精确绑定，不验证引用资料真实性或签名，也不抓取或推断生命周期、交易日历、上市日期或publication完整性。签名/外部审批接口仍是生产门槛。当前只读coverage扫描在SQLite建立显式读事务，在PostgreSQL/MySQL使用事务级REPEATABLE READ；MySQL还要求publication/facts表为事务表，未知或不支持的方言拒绝评估。AFI #68已允许`observed`为`requested`范围内较窄的真实观察区间，因此周末/休市边界不再要求两者完全相等。当前真实数据quality仍为UNKNOWN，指数采集任务也未审核并启用，不能用于生产；此外OD的`COMPLETE`代表从有独立证据的生命周期起点覆盖至请求终点，局部窗口即使内部无缺口也可能被报告为`PARTIAL`，AFI会保守拒绝这类请求。当前仍不能宣称端到端生产可用。创建snapshot的POST不提供幂等键合同，因此SDK不自动重试该POST；每次分页重新验证权限。

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

REST层通过`PUBLISHED_DATASET_COVERAGE_EVIDENCE_FILE`加载严格JSON registry。schema 2 entry必须绑定dataset、`ths.index_daily`来源和32位publication ID，提供唯一规范instrument lifecycle、完整日历片段、半开publication scan范围，以及`visible_record_set`对象（算法、SHA-256、可见行数、publication sequence、合同SHA-256）。旧schema 1可解析但缺少完整集合pin，结果保持UNKNOWN。此文件是可信operator attestation，而非密码学验证：响应中的`operator-attestation-sha256`可标识当次证据内容，但代码只检查结构和pin，证据引用的真实性、来源和审批由独立部署流程负责；服务进程应只读该文件，修改权限应受限于独立审批人员，且不得由API首次扫描目标数据库后生成或更新该pin。它不会从当前行数或最早/最新观察日推断历史边界；新publication不会继承旧publication的coverage证据。没有真实独立证据的环境不要创建entry，留空或schema 1会让结果保持UNKNOWN。即使evidence entry存在，coverage读取仍要求dataset的`coverage.read`授权，并扫描、重新核验该publication sequence下完整可见的fact revision集合及每行record hash；超限、内容变动或无法保证一致读取均返回UNKNOWN。

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
