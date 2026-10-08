# C64：迭代 01 剩余开发与验收

日期：2026-09-30（Europe/Madrid）。开发基线：`d552c081950edffce167e3dfc0be74f37a6950a1`，分支 `dev`，启动时工作树干净。

状态：分钟文件配对备份与仿射复权扩展已完成本地实现及实际隔离数据库演练；源码冻结后的最终完整门禁与覆盖率独立核算均通过。新官方复权对照为 qfq 5 PASS（既有形状容差判定）/ hfq 5 ERROR。迭代整体验收仍为 **INCOMPLETE / NO-GO**；正式部署、其他真实供应商、数据权利与人工确认的证据继续按条目登记。

## 最终冻结门禁与覆盖率

第九遍 `make gate` 完整通过：`GATE_EXIT=0`，耗时 1948.50 秒，源码冻结检查 PASS，1,052 个文件摘要一致；原始输出见 [full-gate.txt](full-gate.txt)，冻结范围见 [final-source-manifest.json](final-source-manifest.json)。全量后端 **3816 passed / 6 skipped / 0 failed / 0 error**（411.26 秒），前端 **15 个文件 / 115 项单元测试通过、27 项浏览器测试通过**（32.0 秒）；ESLint 0 错误 / 33 条既有警告，类型和测试收集检查通过。

110 个注册探针全部复算，682 条反事实均验证，判据、反事实、台账、面基线与成员自证五面通过；`stale-proof=0`。`AC-17|08`、`AC-1|10` 的工作树/历史时刻面按既有规则明确 deferred，需工作树干净后另判；完整门禁退出零不消除这两项边界。

主线程从这一次完整执行的 `.coverage` 独立加权核算，阈值全部达到：

| 范围 | 实测 | 要求 |
|---|---:|---:|
| 全量采集范围 | 89.0376% | ≥84% |
| opendata/data | 91.3690% | ≥90% |
| opendata/pipeline | 90.1363% | ≥90% |
| opendata_fuyao | 94.1482% | ≥90% |
| 默认 A2 差异中已采集运行时代码（180 文件） | 89.4585% | ≥85% |

默认 A2 共 446 个文件；未采集测试 136 个，未采集非测试路径 130 个，非测试路径清单列于 [coverage-final-summary.json](coverage-final-summary.json)。默认采集只涵盖 opendata 和 opendata_fuyao，不把未采集的运维/质量脚本、迁移等路径算作已覆盖。原始报告：[JSON](coverage-final.json)、[XML](coverage-final.xml)、[HTML 压缩包](coverage-final-html.zip)；主线程核算记录 [coverage-final-primary-review.txt](coverage-final-primary-review.txt)。最终 A2 阶段原样保存于 [quality-a2-final-frozen.txt](quality-a2-final-frozen.txt)。

六项历史录制夹具缺失的跳过均为 `tests/test_port_fidelity.py`：`test_ported_output_matches_upstream` 的 stock_daily_raw、stock_daily_qfq、index_daily_em、fund_etf_daily_em 四例，以及 em 的 `test_qfq_factor_steps_are_the_recorded_ex_dates`、`test_qfq_synthesis_reproduces_the_official_series`。没有新增跳过；两套 P0 集成及四个具名 MySQL E2E 都零跳过。上述历史夹具测试不发起新的供应商请求；本轮五标的官方复权真实请求另有完整记录。

本地开发与质量门禁 **PASS**；130 条验收正文及身份保持不变，**100 proven / 30 gap / 0 unreviewed**，迭代整体验收仍为 **INCOMPLETE / NO-GO**。30 项逐条关闭条件见 [remaining-items-audit.md](remaining-items-audit.md)，包括官方双方法复权与价格水平、正式落库/部署运行、数据权利和人工审查、商务联系方式及待决判据。正式仓库写入零，当前工作区变更未提交、未推送、未部署。

## 本轮实现

- 数据目录统一入口，按市场与数据集查看能力，保留函数下钻。
- ODS/DWD 查询、导出、源列名映射；目录声明的 REST 别名接入查询入口。
- 单源公司行动、指数、期货、期权 DWD 迁移及有界、分页、幂等回填；歧义公司行动键在写入前拒绝。
- 统一超时、重试、限流及共享主机并发治理；宏观源接入，缺凭据自动跳过；原始响应缓存与 ODS 水位分别管理。
- 分钟 Parquet 归档、主库元数据、文件查询，保留微秒精度和授权检查。
- 六步流水线检查点、按标的 ODS 水位、恢复、元数据刷新；管理端配置和执行流水线任务；失败清单下载与重试。
- 消费方 Key 生命周期和滚动限流；Redis 配置时使用共享原子计数，不可用时拒绝请求；本地计数有容量上限。
- 双库独立凭据备份、定时触发、失败清理；保留策略默认只报告；P0 巡检、连续失败状态及持久通知队列；上游 Key 异常与运维登记到期时间通知。
- 双库 dump 与分钟文件统一配对快照，全局锁保护分钟索引/文件恢复点，逐文件 SHA-256、完整校验、fsync 和原子发布；清理仅触碰验证完整的过期快照。
- 仿射复权 `scale × 原价 + offset`、现金/送转/配股事件链；原价与旧因子溯源保留，新增六列可空迁移、默认 dry-run 重建、JSON/CSV 部分字段输出、分页和跨批次混版拒绝、有界实际行情键因子读取。
- 修复 pandas 3 时间戳单位回归、真实 JSON 登录限流；更新 QUICKSTART 和消费方示例对接检查。

主代理负责规划、审阅及独立验收；Luna Max 执行代理按路径分工并复用。

## 独立检查

以下检查有重叠，不能相加为全量测试数。完整门禁结果另列。

| 范围 | 结果 | 原始记录 |
|---|---|---|
| 流水线任务、API、调度和重试 | 109 passed | [pipeline-tasks-independent-run2.txt](pipeline-tasks-independent-run2.txt) |
| 水位、恢复、合并、元数据、巡检和运维通知 | 367 passed，2 deselected | [pipeline-ops-independent-final.txt](pipeline-ops-independent-final.txt) |
| 消费方 Key 与限流 | 45 passed，3 deselected | [consumer-key-independent.txt](consumer-key-independent.txt) |
| REST 别名、查询、导出和目录 | 76 passed，15 deselected | [alias-query-independent.txt](alias-query-independent.txt) |
| 落库后真实本地 WS、SDK 缺失隔离、示例取数/小型回测、生产启动配置 | 6 passed | [integration-independent.txt](integration-independent.txt) |
| 隔离 MySQL 恢复、真实本地 API/WS 客户端和 Key | 10 passed | [final-isolated-mysql-resume-client.txt](final-isolated-mysql-resume-client.txt) |
| 分区写入、单源 DWD 与数据库查询 | 13 passed | [isolated-mysql-tests.txt](isolated-mysql-tests.txt) |
| 超窗修订、财务复合键、生产 scoped ODS reader、合并、探针严格解析与原始缓存 | 134 passed，1 deselected | [composite-revision-independent.txt](composite-revision-independent.txt) |
| THS 夹具凭据隔离、路由及 SQLite 引擎范围 | 156 passed，1 deselected | [test-fixture-repair-independent.txt](test-fixture-repair-independent.txt) |
| WS 可达性、前后端端点、日志格式及缓存边界 | 141 passed | [router-cache-tests-independent.txt](router-cache-tests-independent.txt) |
| 配对备份、归档锁、保留校验与 Compose 配置 | 33 passed | [paired-backup-tests-independent.txt](paired-backup-tests-independent.txt) |
| 分钟完整性、锁失败与保留清理、DWD 回填边界、真库写入守卫 | 113 passed | [boundary-tests-independent.txt](boundary-tests-independent.txt) |
| 仿射核心、查询、迁移、CLI、官方对照工具与探针初轮独立回归 | 268 passed，7 deselected；后续有界因子读取由最新集成与最终门禁覆盖 | [affine-focused-independent.txt](affine-focused-independent.txt) |
| 新系数的 MySQL builder 与真实 HTTP 路径 | 4 个具名 E2E 实际 passed，零跳过 | [affine-mysql-e2e-independent.txt](affine-mysql-e2e-independent.txt) |
| 两套独立干净解释器的 P0 集成 | 217 passed × 2，均零失败、零跳过；529 个运行时 Python 文件 SHA 一致 | [clean-env-integration-run.txt](clean-env-integration-run.txt) |
| 前端类型、采集、单元与浏览器完整检查 | 115 单元测试、27 浏览器测试；ESLint 0 错误/33 条既有警告 | [frontend-final-independent.txt](frontend-final-independent.txt) |

独立 MySQL 补跑第一次因未使用精确 E2E 许可值而被 guard 跳过（`isolated-mysql-guarded-attempt.txt`）；零退出码不作为验收。限定本轮隔离库和 10 个节点、使用精确许可值后重新执行，10 项均实际通过。

其他实际证据：

- 主库迁移应用至 `0006`，仓库至 `0006_dwd_daily_legs`：`main-migration-0005.txt`、`main-migration-0006.txt`、`isolated-mysql-tests.txt`。
- 仿射迁移实际从头升级和既有 `0006` 升级、旧值保留、dry-run 零写入、局部重建混版拒绝、非交换现金/送转事件链、幂等、降级再升级通过，见 `affine-mysql-primary.txt`。初轮临时验收夹具标识过长失败保留于 `affine-mysql-primary-fixture-first-failure.txt`；修正限于临时验收脚本。
- 五标的真实源数据只读复制到隔离库：THS 日线 12142 行、公司行动 151 行、DWD 原价 657 行、旧因子 940 行；隔离重建 12140 行 affine，保留旧因子 940 行、基准外事件 97 条单列。正式库写入零，临时库已清理，见 `affine-real-series-primary.txt`。新官方报告 `qfq-official-akshare-affine-real.txt` 为 PASS 5 / FAIL 0 / ERROR 5；qfq 按既有中位数锚定后的形状容差通过，000001 锚定前水平最大偏差 2.55%，五条 hfq 各三次 HTTP 502，因此 AC-11|02 仍 gap。
- 四类 DWD 在自备 MySQL 夹具验证窗口、分页、幂等及追踪列：`dwd-mysql-scenarios.txt`。
- 双库 dump、gzip 校验、恢复精确行数对照：`backup-restore.txt` 是早期 11 表主库的历史检查。新增控制表后，主代理使用实际备份镜像的 MySQL 8.0.46 客户端重新执行双库 dump/恢复：主库 14 表、仓库 18 表，32 表逐表精确行数和两张流水线控制表 DDL 一致，源数据未改变，临时恢复库和凭据已清理，见 `backup-primary-image-restore.txt`。`backup-final-schema-restore.txt` 是执行代理使用 5.7 客户端的补充记录，未替代主代理镜像检查。
- 现有配置仓库只读盘点约 1246 万行、1.92 GB，均为估计空间/行数；GB 表查询与 JSON/CSV 对照通过，写入数零：`warehouse-readonly-inventory.txt`、`warehouse-gb-query.txt`。
- 分钟归档实际 MySQL 元数据与文件 API、微秒精度、损坏文件拒绝：`minute-migration.txt`、`minute-mysql-api.txt`。
- 配对备份使用实际镜像 `opendata-iteration01-backup:paired-acceptance` 的 MySQL 8 客户端：恢复 metadata 14 表/12 行（含 2 个分钟索引）、warehouse 18 表/22 行，逐表一致；恢复 3 个不可变 Parquet 文件后查得 4 行，修订 close=10.5 和微秒时间戳保留，payload/逐文件清单均匹配。原库未改变，临时恢复库/账号/文件清理，见 `backup-paired-minute-primary-restore.txt`。两次验收脚本 `%` 主机字面量参数格式错误保留在 `backup-paired-minute-primary-first-failure.txt`、`backup-paired-minute-primary-paramstyle-failure.txt`，修复仅在临时验收脚本。
- 真实临时栈 JSON 登录、目录及第 11 次登录限流：`startup-login-health.txt`。`frontend-real-stack.png` 是早期界面历史截图，含后来删除的重复导航；当前导航由后续测试与门禁检查。

## 质量修复与证据边界

首次全量测试 `baseline-tests.txt` 有 5 项失败，原始输出保留。首次完整 A2 输出 `quality-a2-first-failure.txt` 也保留；默认 A0 差异范围，未使用 `A2_BASE_REF=HEAD` 缩小范围。

完整门禁第一遍在日志格式检查发现标准库 logging 的 `extra` 控制参数误报，已修复并验证反例；第二遍发现 5 项旧证明与当前代码不一致。原始失败输出分别保留在 `gate-run1-failed-loguru.txt`、`gate-run2-failed-probes.txt`。查询代码的最新真实只读执行计划见 `window-pruning-explain.txt`：现有 6 张分区表全部裁剪分区，其余未落地域单独报告。

第五遍门禁在探针阶段发现 4 项证明绑定失配，失败输出见 `gate-run5-failed-named-bindings.txt`：两条 WS 用例改名后缺少具名绑定，另三项因默认缓存缺 Chromium 而未启动浏览器。恢复原具名测试绑定并安装锁文件对应浏览器到本轮专用目录后，4/4 探针独立通过，18 条反例均打回 gap，见 `four-bindings-independent.txt/.json`；未改产品阈值。

第六遍在证据跟踪检查失败：运行期间新增的原价诊断档案尚未进入 Git 跟踪清单，见 `gate-run6-failed-untracked-diagnostic.txt`。第七遍冻结检查通过，但 AC-11|04 的 EXPLAIN 源码摘要已过期、AC-16|07 的新双环境日志缺少既有探针要求的总退出码字段，见 `gate-run7-failed-current-proof-bindings.txt`、`gate-run7-source-manifest.json`。重新执行当前代码的真实只读 EXPLAIN，6 张现有分区表全部裁剪；双环境各 217 项重新通过并由成功执行记录 `CLEAN_RUN_EXIT=0`。旧档案保存在 `window-pruning-explain-before-affine.txt`、`clean-env-integration-affine-before-exit-contract.txt`。EXPLAIN 临时验收包装器首次配置属性名错误保存在 `window-pruning-explain-refresh-first-failure.txt`，修正仅在临时验收脚本；产品代码、判定式和阈值未改。

第四遍门禁因完整性补审确认分钟文件配对备份尚缺实现而主动取消，原始退出 2 输出保留在 `gate-run4-cancelled-minute-backup.txt`；该轮 A2 阶段已通过，但不代表整个门禁通过；`quality-a2-final-frozen.txt` 在最终门禁完成后仅保存最终一轮阶段结果。

第八遍完整门禁的源码冻结检查通过，110 个探针均完成、过期证明为零，但全量后端为 3813 passed / 2 failed / 6 skipped，见 `gate-run8-failed-backend.txt`。两项失败分别是别名测试把新增加的复权版本预查 SQL 与行情 SQL 比较，以及 CLI 测试中 SQLite 夹具未满足全局引擎守卫。修复仅涉及两个测试文件：分别对比两条预查和两条行情 SQL；SQLite helper 仅接受本地 Path，并用负例确认任意数据库 URL 在创建引擎前被拒绝。原失败和覆盖率诊断保留；主线程在冻结运行时代码后独立复跑三个完整模块，40 passed，见 `alias-sqlite-guard-primary-after-fix.txt`。完整门禁需从头重跑，不以局部结果代替。

冻结副本的全量后端诊断发现 19 项失败，原始输出与源码清单保留在 `prebackup-backend-diagnostic-failed.txt`、`prebackup-source-manifest.json`。THS 正向夹具明确自身测试凭据，真正缺凭据负例保持；SQLite helper 拒绝任意连接串；FastAPI 新版延迟路由使用实际 WS 行为及 OpenAPI 操作表核对；标准库日志的真实格式失败使用私有 handler；干净 P0 档案重新执行两个解释器，未降低阈值或排除门禁用例。独立复跑结果见上表。

随后全量后端 3689 passed / 6 skipped，记录在 `final-backend-before-gate.txt`。6 项跳过均来自历史缺失的移植对照夹具，测试只读录制文件，不执行新网络请求；P0 集成两环境均零跳过。该轮总覆盖率 88.40%，data 89.18%、pipeline 89.92%，未满足关键包 90% 下限，因此另补分钟归档及回填失败路径。主线程独立复跑 113 项通过；局部覆盖导出继承全套 84% 下限而退出 2，原记录保留，随后仅以 `coverage json --fail-under=0` 导出诊断 JSON（没有改动门禁下限）。与先前全量结果的集合合并仅作为估算，见 `boundary-coverage-projection.json`；当时尚需完整门禁重新实测；当前最终实测见上方“最终冻结门禁与覆盖率”。

覆盖率口径为 `(covered_lines + covered_branches) / (num_statements + num_branches)`。
默认 `make test-cov` 只采集 opendata 与 opendata_fuyao；A2 差异中的已采集运行时代码、
未采集测试和未采集非测试路径分别记录，见 `pre-gate-coverage-scope.json`。报告没有把
未采集的质量/运维脚本及迁移当作已覆盖，也没有对这些路径声明 85% 达标。

第三遍门禁因随后复现财务复合键漏读而主动取消，退出 2 的完整输出保留在 `gate-run3-cancelled-source-repair.txt`，不算完整门禁通过。日期索引现由 mapping.key 中的契约日期字段推导；真实 SQLite 财报/财务指标用例分别覆盖索引 2/1，并排除同日无关科目/指标。修复前失败输出见 `composite-revision-before-fix.txt`，主线程独立检查见 `composite-revision-independent.txt`。

复核发现生产 scoped ODS reader 在缩小水位窗口后漏读窗口外修订键；真实 SQLite 链路另复现字符串日期比较失败，原始输出见 `scoped-revision-before-fix.txt`。修复后单源/多源、None 水位和无关标的排除均有实际查询与改值检查。5 项旧证明重新复算全部 proven，48 条定向反事实均能打回 gap，见 `repaired-five-probes.txt/.json`。探针同组节点改为一次 pytest 启动、私有 JUnit 文件逐节点严格判定；没有跨源码缓存，缺失/跳过/失败/重复/意外参数扩展继续拒绝通过。

干净环境第一次从当前工作区构建 wheel 时，历史忽略目录 `build/lib/akshare` 混入 wheel，实际输出保留在 `clean-env-build-contaminated.txt`。用户旧构建目录保留；使用 git 管理及非忽略的当前文件构造独立源码副本重建 wheel 后，上游包均不可导入、依赖一致性检查通过，见 `clean-env-build.txt`。验收依赖在 `dev` extra 中声明 `backtrader`，使 CI 能实际执行小型消费方回测。

完整 A2 暴露 pandas 3 / 类型声明兼容问题。采用具体 DataFrame/契约行类型边界 cast、局部变量区分和合法文本读取；没有增加 blanket ignore 或移出检查面。C16/C19/C25/C26/C27/C37/C54 的历史 **Python 源码**也作了类型修复，历史原始 `.txt`、`.json` 输出未改写，旧真机探针未执行。

所有宿主机 Python 由用户 Anaconda `base` 调用，临时独立解释器也由该 Anaconda 创建。备份镜像使用内置 Python 3。早期检查使用继承 base 的固定工具环境；最终门禁使用不继承全局包、只安装声明依赖的独立环境。开发 base 原有上游包保留；仿射扩展后的两套干净环境从同一独立源码 wheel 安装，四个运行时包 529 个 Python 文件 SHA-256 逐个匹配，依赖一致性通过，集成面均实际 217 passed / 0 failed / 0 skipped，见 `affine-clean-environments-refresh.txt`、`clean-env-integration-run.txt`。此前 202 项双环境原始输出保存在 `clean-env-integration-before-affine.txt`；早期环境盘点失败见 `clean-env-integration-upstream-present.txt`。独立环境另暴露 Requests 类型版本差异，两处精确边界修复后重新检查，失败输出保留在 `quality-a2-clean-first-failure.txt`。

常规门禁关闭调度、Redis 和 live e2e；数据库写入仅使用本轮独立 MySQL 8.0.46 容器 `127.0.0.1:33564`，现有仓库只读。主代理额外启动自有、无 TCP/持久化的临时 Redis，两个独立 OS 进程实测同一 Key 的一分钟共享限额（16 次并发请求允许 5/拒绝 11）、不同 Key 隔离、TTL、服务器时钟过窗和断连拒绝通过，见 `redis-two-process-independent.txt`；旧客户端清理接口失败单独保留。该检查没有部署真实多 worker 应用；通知至少一次交付，真实 SMTP 交付未验。

新增复权扩展的已确认口径、兼容字段、锚点、事件歧义拒绝、迁移和实际验收边界见 [affine-adjustment-design.md](affine-adjustment-design.md)。搬运层此前未处置的五个 Bandit 发现已有源码审阅材料 [ported-security-review-draft.md](ported-security-review-draft.md)，仍待人工 triage，不据此宣称通过。

## 待关闭事项

当前 **100 proven / 30 gap / 0 unreviewed**，100 个勾选与 19 行分组计数已对账。40 项本轮新增判定已复算，原有证据继续保留其日期和边界。110 个注册探针进入门禁；原有 7 项没有专门探针的历史记录仍由对应质量子项/测试及原始档案证明，未扩大这项历史豁免。

逐条状态以 [验收台账](../../quality/acceptance-item-ledger.json) 为准，未通过项及关闭条件见 [remaining-items-audit.md](remaining-items-audit.md)。商务联系信息的待确认项和已确认的仿射复权口径见 [acceptance-decisions.md](acceptance-decisions.md)。

本轮已执行上述五标的官方复权真实对照；其余供应商覆盖按台账保留缺口。未提交、推送、部署、执行旧表 DROP 或外发通知，正式仓库写入零。

复现见 [reproduce-local-acceptance.md](reproduce-local-acceptance.md)。原始日志尾随空格保留，源码差异检查分别报告。

本轮自建 MySQL33564 容器及两个备份镜像标签已清理；源码、原始证据和临时干净解释器/浏览器运行时保留，见 [final-owned-resource-cleanup.txt](final-owned-resource-cleanup.txt)。停止后的首轮即时观察失败档案独立保留，随后重新盘点确认容器已移除，再校验镜像 ID 并清理。最终源码、判据和交付范围核对见 [final-delivery-review.txt](final-delivery-review.txt)。

最终档案复核首次发现 `quality-a2-final-frozen.txt` 缺提交身份头，输出保存在 [final-evidence-traceability-before-identity-header.txt](final-evidence-traceability-before-identity-header.txt)，原摘录在带归档说明的 [quality-a2-final-before-identity-header.txt](quality-a2-final-before-identity-header.txt) 中逐字保留。仅补齐最终摘录的提交 SHA 和命令头；A2 原始阶段输出、产品代码和判定阈值不变。最终跟踪复查见 [final-evidence-traceability-review.txt](final-evidence-traceability-review.txt)。
