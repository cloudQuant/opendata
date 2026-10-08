# C64 未通过条目复核

日期：2026-09-30。结论：**INCOMPLETE / NO-GO**。全部条目身份和判据正文保持原样；只在取得匹配的独立证据后翻勾。

本轮完成 53 项既往未审条目的复核，其中 40 项经已注册探针最终复算通过，13 项确认仍缺证据；保留原 17 项缺口。当前 **100 proven / 30 gap / 0 unreviewed**，完整门禁另报。

## 关闭条件

- 真实供应商：P1 对照覆盖、已启用腿逐源对照、双源全市场和四件套抓取、交易日调度校准及真实 Key 配额/到期信息。
- 数据与性能：全量单源落库时间/RSS、全市场十年恢复任务；原有大型表查询通过不能替代写入基准。
- 人工与权利：商务联系信息、数据权利与 clean-room/逐 provider 审查、口径映射签认和请求方确认。复权公式扩展已由用户确认，不再是待决事项。
- 正式运维：新年度分区/单源 DWD 正式落库证据、持续备份/RPO、binlog 重放及分钟配对备份的生产部署、真实通知交付。分钟配对备份的本地代码与实际镜像恢复演练已补齐，见 `backup-paired-minute-primary-restore.txt`；该演练不替代生产运行证据。
- 旧库：旧表对应清单和只读权限；保留与 DROP 冲突先作决定。现有表本轮未写入或删除。

## 逐条缺口

每条完整理由与历史证据以 `docs/quality/acceptance-item-ledger.json` 为准。以下为本轮可读摘要，既往原始记录未改写。引用旧轮次的文件数量和命中数量属于该档案当轮读数；本轮当前运行时扫描面为四包 529 个 Python 文件，原有三个名称字符串基线仍保留。

### AC-1|03

判据：**裸词 `akshare` 的逐词白名单**（该词是上游库与数据源名，必须保留在署名与集成层）：`akshare/`（迭代 A2 起为 `opendata_http/`）、`THIRD_PARTY_NOTICES.md`、`LICENSE-AKSHARE`、`README.md` 与 `CODE_QUALITY.md`（沿革引用）、`scripts/codemod/`、`tests/`、`docs/`（含 `docs/evidence/` 的逐字命令输出）。集成层 `import akshare` 的清除属 FR-17（1B-B5），其存量由 AC-16 的基线冻结、只降不升

缺口及关闭条件：裸词 akshare 命中 611 个跟踪文件，其中搬运树 opendata_http/ 315；**条目白名单之外仍有 70 个**：适配层包 opendata/data/providers/akshare/ 15 个 + 55 个（opendata/data 注册表与映射 11、scripts/ops 跨供应商对照脚本 8、opendata/pipeline 5、scripts/quality 4、opendata/api 3、core/models/data_fetch/utils/services/main/cli 11、根文档 ARCHITECTURE.md+QUICKSTART.md 2、构建运维面 Makefile/Dockerfile/.pre-commit-config.yaml/bandit.yaml/pyproject.toml/requirements.txt/.env.example 7、frontend 2、alembic_data/versions 1、opendata_fuyao/endpoint_map.yaml 1）。条目列的 7 个面写于 A0，A2 改名与 B5 去依赖之后从没跟上：白名单该不该覆盖适配层与对照脚本是**措辞决策**，不由本轮替用户定。另一处如实登记：从条目原文回读白名单时，`akshare/` 与括注 `opendata_http/` 被解析成同一条（10 条里有 1 对重复）。集成层 import akshare 的存量…（完整理由见台账）

证据：`docs/evidence/C40/probe-all.txt`、`docs/evidence/C40/probe-readings.json`、`docs/evidence/C40/README.md`、`docs/quality/zero-dep-baseline.json`

### AC-1|05

判据：MySQL 双库名为 opendata / opendata_data，**四处配置面一致**（`config.py` 默认值、`.env.example`、`docker-compose.yml`、`init.sql`）；后端启动、前端登录、/health 均正常

缺口及关闭条件：本轮真实 backend 启动、JSON 登录、目录与第11次登录限流检查通过；Chromium 真实前端登录并获取目录也通过。检查使用独立端口和本轮临时 MySQL 库，/health 返回200且 database=connected；scheduler 显式关闭时总体 degraded。本轮没有声称正式部署或默认库名上的 healthy 栈已经验收，原静态 runtime 探针也未改为信任临时实例档案。

证据：`docs/evidence/C64/startup-login-health.txt`、`docs/evidence/C64/frontend-real-stack.txt`、`docs/evidence/C64/README.md`

### AC-1|07

判据：LICENSE 为 BSL 1.1 且四要素齐备；`LICENSE-AKSHARE` + `THIRD_PARTY_NOTICES.md`（含资源权利登记）；README 含授权说明、商务联系方式与**数据免责声明**

缺口及关闭条件：许可面全绿：LICENSE 以 Business Source License 1.1 开头、参数块五键齐（缺 0/四要素，Change Date 2030-09-22、Change License MIT）、LICENSE-AKSHARE 为 MIT、THIRD_PARTY_NOTICES.md 四节含嵌入资源权利块、README 授权说明与数据免责声明均在。唯一卡点是判据明写的『商务联系方式』：README L160 现在是自标占位的 `cloud@example.com（占位，发布前替换为正式联系方式）`（placeholder=yes）。这是要用户提供的文本，编一个能过判据的邮箱就是造假，故不勾。

证据：`docs/evidence/C40/probe-all.txt`、`docs/evidence/C40/probe-readings.json`、`docs/evidence/C40/README.md`、`LICENSE`、`THIRD_PARTY_NOTICES.md`

### AC-1|08

判据：**数据源权利登记表**存在且覆盖全部数据源（来源/条款链接/允许用途/复核日期/责任人）

缺口及关闭条件：登记表存在且列齐（16 行 × 十列，含复核日期与责任人两列），覆盖对账也过：provider-inventory.yaml 引用的 10 行全部有对应行、无悬空引用。但『覆盖全部数据源（来源/条款链接/允许用途/复核日期/责任人）』按内容量 today：复核日期真填 **0/16**、允许用途仍答『待确认』 **11/16**、条款链接写成散文而非 URL **13/16**。列在不等内容在——填这些是法务/产品复核结论，本轮能做的只是把它变成三个可复算的数（dated/undecided/unlinked），已做。

证据：`docs/evidence/C40/probe-all.txt`、`docs/evidence/C40/probe-readings.json`、`docs/evidence/C40/README.md`、`docs/data-rights-registry.md`

### AC-1|09

判据：**凭证未入版本库**：`git ls-files | grep -E '\.env|\.idea|\.pid'` 为空；`gitleaks detect` 全历史扫描 0 命中（白名单仅含文档/模板）；上游硬编码凭证已处置并登记——`akshare/stock/cons.py`、`akshare/bond/bond_convert.py`、`akshare/bond/bond_china_money.py`、`akshare/futures/futures_hf_em.py`、`akshare/option/option_em.py`

缺口及关闭条件：全历史扫描器今天实跑通过（make secret-check exit 0，`no leaks found`），上游 5 个硬编码凭证文件登记 5/5、残留凭证形状字面量 0。两条仍未满足：①条目字面的 `git ls-files | grep -E '\.env|\.idea|\.pid'`读出 **1 条 `.env.example`**（按真实凭证形状重跑 `(^|/)\.env$`、`.idea/`、`*.pid` 为 0）——这与 AC-1|03 明文允许 .env.example 作为模板面**互相矛盾**，要么改条目措辞要么删模板；②『白名单仅含文档/模板』按今天不成立：.gitleaks.toml 全局豁免 2 条中非文档/模板 1 条（`\.egg-info/`，构建产物），另有 2 个 per-rule 豁免块（C36 那条形状豁免的代价已由反事实量出：全大写带点的 4 段值会被放过）。本轮把 C36 的 reason 更新为这一版（C36 只说了豁免面，没说两条判据互斥）。

证据：`docs/evidence/C40/probe-all.txt`、`docs/evidence/C40/probe-readings.json`、`docs/evidence/C40/README.md`、`.gitleaks.toml`、`docs/evidence/C36/gitleaks-counterfactual.log`、`docs/evidence/A0/secret-audit.txt`

### AC-5|02

判据：迭代 1A：P0 域子模块搬运完成；迭代 1B：P1 域子模块搬运完成

缺口及关闭条件：「P0 域子模块搬运完成 / P1 域子模块搬运完成」缺一个机器可读的分母：`opendata/data/domains.yaml` 与 `Capability` 都不带 batch/priority/tier 字段，需求 D9 与实施计划 B1.1 的子模块清单写在「stock、stock_feature、…、utils **等**」这类散文里，所以「完成」无法从树上判。已量的两半是实的：首方代码以 ``opendata_http.<name>`` 取用的 11 个端点函数聚合层全部导出（缺 0）、被点名移出本迭代的 8 个非金融子包一个都没混进来（0）；D9 点名范围内真正没搬的只有 `futures_derivative` 1 个，而它是否属于 P0/P1 也要同一份字段来定。补齐路径写进探针 repair：`tier_field: - -> opendata/data/domains.yaml#batch` + `scope_missing: 1 -> 0`。**谁声明哪个域属哪一批是产品/接口决策，本轮不替它编字段。**另登记仪器缺陷：第一版把散文里的「utils 等」切成一个子包名，量出「缺失 2 个」其中一条是解析残渣，修法是剥掉行尾「等」再比（不是把 utils 特判掉），修后 readings 见 docs/evidence/C61/probe-items-ac5.txt；本格结论没变，但 gap 的理由必须是真的。

证据：`docs/evidence/C61/README.md`、`docs/evidence/C61/probe-items-ac5.txt`、`docs/evidence/C61/probe-self-test.txt`、`docs/迭代计划/迭代1-重构数据中台/需求文档.md`、`opendata/data/domains.yaml`

### AC-5|03

判据：codemod 差异报告人工待办清零；**AST 级静态扫描**零 `akshare` 运行时引用（含字符串常量与动态导入）

缺口及关闭条件：「codemod 差异报告人工待办清零」这一半到了（docs/port-report.md 自报待办 0 条、清单段落首行「（无 —— 待办清零）」且无挂着的复选项）；没到的是「AST 级静态扫描零 `akshare` 运行时引用（含字符串常量与动态导入）」：`scripts/codemod/verify_no_akshare.py` exit 0 且 --self-test exit 0，import 形态 0、动态导入 0，**字符串常量形态 3 条**并正是冻结基线里那 3 条产品事实的名字面量（opendata/data/openbb_map.py、opendata/models/data_script.py、opendata/pipeline/patrol.py；扫描器原话 517 file(s) walked，opendata_http=313）。抹平它只有两条路，两条都要改判据本身：①把扫描器字符串规则改窄＝回头放宽已勾的 AC-16|05「AST 口径」；②改掉这三处真实数据源名的拼写＝给扫描器做伪装，正是 C35 收口过的门禁空转。**同一道题 AC-16|06 已登记为用户/产品决策（task #64）**，这里不重复改判、也不换个说法再判一次。

证据：`docs/evidence/C61/README.md`、`docs/evidence/C61/probe-items-ac5.txt`、`docs/evidence/C61/probe-self-test.txt`、`docs/quality/zero-dep-baseline.json`、`scripts/codemod/verify_no_akshare.py`

### AC-5|07

判据：**搬运层安全扫描**（bandit 含 B 层）完成并人工 triage 留档

缺口及关闭条件：判据两半：扫描在面（在）与人工 triage 留档（这份留档已经跟不上树）。本轮自写全量档案 docs/evidence/C61/ported-bandit-scan.json（archive_round=C61、generated_at=2026-09-28T20:33:11Z、命令 bandit -q -r opendata_http -f json）：1021 项 / 234 个文件带 finding / 13 条规则（B105,B107,B110,B112,B113,B301,B307,B311,B324,B403,B404,B501,B603），全是 B 层且文件全在搬运清单内。而 docs/evidence/A2/bandit-ported-triage.md 自己声明扫的是 131 个 py 文件 / 328 项、处置 10 条规则，搬运树今天是 313 个 py 文件 ⇒ 覆盖面不等，**5 项落在 triage 从未处置过的规则上**：B403@futures/cons.py:13、B301@futures/cons.py:576、B324@stock_feature/stock_a_indicator.py:50、B324@stock_feature/stock_info.py:36、:37。那句免罪句「无 B608（SQL 拼接）、无 B324（弱哈希）、无 pickle/yaml.load 类反序列化项；builtin exec 站点 0」描述的是当初那棵…（完整理由见台账）

证据：`docs/evidence/C61/README.md`、`docs/evidence/C61/probe-items-ac5.txt`、`docs/evidence/C61/probe-self-test.txt`、`docs/evidence/C61/ported-bandit-scan.json`、`docs/evidence/A2/bandit-ported-triage.md`、`docs/evidence/A2/bandit-ported.json`、`Makefile`、`bandit.yaml`

### AC-6|02

判据：迭代 1B：其余子模块抽样 ≥20% 对照通过；产出对照报告（覆盖率/差异清单/容忍度说明）

缺口及关闭条件：P1 ≥20% 抽样的合格案例分母、覆盖与完整对照未闭合，Eastmoney 仍有网络待判。按启用范围补覆盖、差异与容差报告。

证据：`docs/evidence/A2/compare-report.md`、`docs/evidence/B1/README.md`

### AC-8|04

判据：**写入基准测试**记录在案（单源日线全量落库耗时与内存）

缺口及关闭条件：既有基准仅 20000 行耗时，缺单源全量规模及峰值 RSS。需在隔离数仓记录全量写入规模、耗时和内存。

证据：`docs/evidence/A4/write-benchmark.txt`、`docs/evidence/C58/README.md`

### AC-8|05

判据：**分区**：大表按 `RANGE COLUMNS(trade_date)` 年分区 + MAXVALUE 兜底；**分区维护任务**可运行

缺口及关闭条件：一次性MySQL已迁移0006，并验证多类日线表；现有配置仓库只读盘点仍是12表，未执行迁移。历史C57样本不能冒充C58完整注册表分区普查；正式仓库全量分区覆盖尚未证明。

证据：`docs/evidence/C64/isolated-mysql-tests.txt`、`docs/evidence/C64/warehouse-readonly-inventory.txt`、`docs/evidence/C64/README.md`

### AC-8|06

判据：**跨年写入用例**：模拟新年度数据写入成功（无"no partition for value"错误）

缺口及关闭条件：一次性MySQL跨年写入用例通过，并已改为真实PARTITION(p2027)与pmax精确count，未用information_schema估算行数作写入证明。尚无正式部署完整注册分区集的当前跨年写入证据，保留gap。

证据：`docs/evidence/C64/isolated-mysql-tests.txt`、`docs/evidence/C64/README.md`

### AC-8|08

判据：真机：**A 股日线**双源（ths + akshare）全市场落 ods 两表（迭代 1A）；**四件套**双源（迭代 1B）

缺口及关闭条件：只读盘点不证明双源全市场实际抓取；旧 akshare 表部分来自迁移。需双源及四件套真实抓取、落库和来源/标的/行数/批次证据。

证据：`docs/evidence/B1/README.md`、`docs/evidence/C64/warehouse-readonly-inventory.txt`

### AC-9|08

判据：**单源域 dwd 直通模式可用**（`layer=dwd` 查询不报错）

缺口及关闭条件：新增有界单源DWD回填CLI与指数/期货/期权0006迁移；公司行动与三类日线在一次性MySQL夹具上窗口、分页、追踪、幂等通过，歧义公司行动键在首写前拒绝。现有仓库只读盘点仍显示公司行动DWD=0；未执行现有仓库回填或人工gold样本审阅。

证据：`docs/evidence/C64/dwd-mysql-scenarios.txt`、`docs/evidence/C64/warehouse-readonly-inventory.txt`、`docs/evidence/C64/README.md`

### AC-10|01

判据：**已启用** provider 全部注册且对照通过（**不设"32 个全部完成"作为门槛**；未启用者标注 `notes="on-demand"`）

缺口及关闭条件：启用清单仍含 registered 腿而非每腿对照通过。需补启用腿对照，或由产品决定移出未启用项并标 on-demand。

证据：`opendata/data/openbb_map.yaml`

### AC-10|02

判据：每个 provider 标注"消费场景 + 请求方"；标不出场景的移出本迭代

缺口及关闭条件：消费场景已记录，但启用项缺具名请求方。需产品/消费方负责人补齐并确认，或移除无请求方项目。

证据：`docs/proposals/openbb-migration/provider-inventory.yaml`

### AC-10|03

判据：每 provider 零 OpenBB 源码（审查记录留档）

缺口及关闭条件：逐 provider 人工源码审查档案仍为 0/7。需逐包审查范围、依据、审查人及结论；静态搜索不能替代人工作业。

证据：`docs/evidence/C62/README.md`

### AC-11|02

判据：**`adjust=qfq|hfq` 服务端计算**：同一标的 qfq 序列与 akshare 官方 qfq 对照一致（容忍度内）

缺口及关闭条件：用户已确认并实施 `scale × 原价 + offset`，保留原价与旧因子溯源。主线程实际 MySQL 检查了两条迁移路径、dry-run、现金分红与送转事件链、混版拒绝、幂等和降级再升级；查询/导出与 official checker 的本地 offset 控制也已纳入回归。真实五标的 THS 源数据只读复制到隔离库后，重建了 12140 行 affine，保留 940 行旧因子，97 条基准外事件单列。新官方对照 qfq 5 PASS / hfq 5 ERROR（每条三次 HTTP 502），FAIL 0、整体退出 1；原容差不变。qfq 的 PASS 采用既有中位数锚定形状判定，000001 锚定前价格水平最大偏差仍为 2.55%。该标的 DWD 与 THS raw 共 131 根完全一致，官方 raw 另一次请求失败、未取得读数，不能据此宣称官方原价一致。THS 原价基准截至 9 月 23 日，9 月 24 日现金事件在基准外；剩余条件包括补齐事件后的原价基准、重建正式仓库、复核价格水平，以及完成五标的双方法零 FAIL/ERROR 的官方对照。C56 历史对照仍为 PASS 0 / FAIL 1 / ERROR 9；28.02 是历史价格序列推断，未当作本轮独立核对的源事件金额。

证据：`docs/evidence/C64/affine-adjustment-design.md`、`docs/evidence/C64/affine-mysql-primary.txt`、`docs/evidence/C64/affine-mysql-e2e-independent.txt`、`docs/evidence/C64/affine-real-series-primary.txt`、`docs/evidence/C64/qfq-official-akshare-affine-real.txt`、`docs/evidence/C64/affine-bank-raw-attribution.txt`；历史对照 `docs/evidence/C56/qfq-official-akshare.txt`、`docs/evidence/C56/qfq-attribution.txt` 保留。

### AC-13|08

判据：**调度时间业务校准**完成（真机实测调窗，避免"成功执行但没拿到当日数"）

缺口及关闭条件：旧 15:01 无当日 K 的采样及 cron 注册不证明业务校准。需交易日带时间戳采样和实际调度取到当日 K、ODS 新鲜度推进。

证据：`docs/evidence/B3/schedule-calibration.txt`、`docs/evidence/B4/README.md`、`docs/evidence/C12/README.md`

### AC-15|01

判据：转移评估前旧表保留只读未破坏

缺口及关闭条件：当前数仓盘点未对应全部待保留旧表，未证明其访问权限只读。需正确旧库的表/权限/结构/行数基线和未写入记录。

证据：`docs/evidence/C64/warehouse-readonly-inventory.txt`、`docs/evidence/C23/README.md`

### AC-15|02

判据：完成转移评估：有增量价值的数据经迁移脚本 upsert 进 ods（key 冲突以新抓为准），行数与抽样比对一致后 DROP 旧表；无增量价值则归档后 DROP

缺口及关闭条件：历史决定旧表只读、不 DROP，与验收要求冲突。需用户决定保留并修订口径，或授权迁移/归档/删除后提供行数、抽样及迁移后盘点；本轮不执行旧表删除。

证据：`docs/evidence/C23/README.md`

### AC-19|01

判据：**备份策略落地**：每日备份 + binlog，RPO ≤24h〔C63 做成条目级判定面（measure_ac19_01 / judge_ac19_01 + 6 条反事实），**不勾**：判据要的是「每日备份」，而现场唯一缺的就是那个「每日」。读数五面分开——脚本面：`scripts/ops/backup_mysql.sh` 在跟踪清单 = yes，`dump_one` 调用 2 次（元数据库 + 仓库库各一次全量）、`mysqldump` 真实调用 1 处（前置检查那行不计，判定式按语句而不是按字面出现次数）；调度面：**每日触发点 0 处**，扫描人口 1625 个跟踪文件（`tests/`、`docs/`、`web/`、脚本自身与探针自身已排除），仓库里没有任何调度定义或 make 目标去跑它，只有脚本头部注释里那行 `0 2 * * *`…（完整判据见验收文档）

缺口及关闭条件：每日触发源码和隔离双库恢复已验证，隔离 ROW binlog 取值=yes；正式部署中每日任务运行、备份新鲜度及 binlog 实际生效证据=no，生产 RPO ≤24h 尚未验收

证据：`docs/evidence/C64/final-live-gaps.txt`、`docs/evidence/C64/final-live-gaps.json`、`docs/evidence/C64/README.md`

### AC-19|04

判据：权威源 Key 配额/到期/封禁监控告警生效〔C63 做成条目级判定面（measure_ac19_04 / judge_ac19_04 + 7 条反事实），**不勾**：判据的动词是「生效」，现场是分类在位而信号与通道缺。四类健康类别 4/4（key-validity / key-expiry / revocation-or-ban / quota-left），线上可触发的被动归类两条都在（401/403→credential-rejected = yes、429→quota-exhausted = yes；判定按 if/elif/return 语句里的成对规则，不按注释里的字符串）；**到期这一类没有任何可触发的信号 = no**——本平台自签 Key 的 `is_expired` 与 `token_blacklist` 不算，判据点名的是**权威…（完整判据见验收文档）

缺口及关闭条件：判据要类别、可触发信号、分级落点、通知通道与观察状态测试共同成立；当前读到 4/4 类，operator expiry support=yes，分级落点=2、通知通道=1，observation tests=3/3。观察 TTL 过期不能替代 key 恢复，quota 余量未知时也不能填入数值；还缺供应商到期/余量和实际通知送达的独立现场证据

证据：`docs/evidence/C64/final-live-gaps.txt`、`docs/evidence/C64/final-live-gaps.json`、`docs/evidence/C64/README.md`

### AC-16|01

判据：代码审查记录：全部自研 provider 均有"独立编写 + 无源码参照"审查留档〔C62 做成条目级判定面（measure_ac16_01 / judge_ac16_01 + 5 条反事实），**不勾**：这条缺的不是读数，是那份留档本身。现场读数——git 跟踪清单里带 `registration.py` 的自研 provider 包 **7 个**（akshare、ecb、fred、imf、oecd、ths、yfinance），提交说明带「无 OpenBB 源码参照」的 **0/7**；`docs/evidence/` 里同时写得到该句与「审查」字样、并按包名点名该包的留档 **0/7**。另有 5 个包把 `no OpenBB code was consulted` 写进自己模块的 docstring——那是代码里的自声明，按字面不认作审查记…（完整判据见验收文档）

缺口及关闭条件：判据要「全部自研 provider 均有审查留档」，现场是逐包 0/7：git 跟踪清单里带 `registration.py` 的自研 provider 包 7 个（akshare、ecb、fred、imf、oecd、ths、yfinance），提交说明带「无 OpenBB 源码参照」的 0 个，`docs/evidence/` 里同时写得到该句与「审查」字样并按包名点名该包的留档 0 个。规则文本在位（`docs/proposals/openbb-migration/README.md` 的留痕要求 + 该文档把 AST 断言扩展到「零 openbb」）、模块 docstring 里的 `no OpenBB code was consulted` 自声明覆盖 5 个包，但两者都是**一次性全库断言**，不是「全部 provider 均有」的逐包记录，按判据字面不认。历史提交说明补不回来（不改写历史），收口要一次真实的人工逐包审查留档，本轮不代拟。

证据：`docs/evidence/C62/README.md`、`docs/evidence/C62/probe-items-ac16.txt`、`docs/evidence/C62/self-test.txt`、`docs/evidence/C62/guards.txt`、`docs/evidence/C62/probe-check-after-fix.txt`、`docs/evidence/C62/gate-run1.txt`、`docs/evidence/C62/gate-run2-final.txt`

### AC-16|02

判据：全库无 OpenBB 源码或其近似复制（人工抽查 + 提交声明核查）〔C62 做成条目级判定面（measure_ac16_02 / judge_ac16_02 + 6 条反事实），**不勾**：判据括号的否定面这轮第一次量全，且**正向对照先行**——运行时四包（`opendata`、`opendata_http`、`opendata_fuyao`、`opendata_client`）git 跟踪的 py 文件 517 个、AST 逐个解析成功 **517 个**，共读到 **88 个**不同的顶层 import 根（走查不是空转），其中 openbb / openbb-* 的 **0 个**；`scripts/codemod/verify_no_akshare.py` 的 `FORBIDDEN_ROOTS` =（akshare, openbb）含 …（完整判据见验收文档）

缺口及关闭条件：否定面本轮第一次量全并带正向对照：运行时四包 517 个跟踪 py 文件逐个 AST 解析成功 517 个，读到 88 个不同顶层 import 根，其中 openbb / openbb-* 0 个；`verify_no_akshare.py` 的 `FORBIDDEN_ROOTS` 含 openbb 且正文有 5 处 openbb（含 `--self-test` 故意违规样本，即这条断言会响）。字面出现 openbb 的运行时文件 6 个，全是 FR-7 对照表加载器与 provider docstring 的 clean-room 自声明，无源码。但判据括号点名的两项核查都没做过：提交声明核查 0/7、逐包人工抽查留档 0/7；「近似复制」只能靠逐包人工比对判定，不以「import 为零」冒充已被抽查过。

证据：`docs/evidence/C62/README.md`、`docs/evidence/C62/probe-items-ac16.txt`、`docs/evidence/C62/self-test.txt`、`docs/evidence/C62/guards.txt`、`docs/evidence/C62/probe-check-after-fix.txt`、`docs/evidence/C62/gate-run1.txt`、`docs/evidence/C62/gate-run2-final.txt`

### AC-16|06

判据：集成层的 `import akshare` 残留（FR-17 于 1B-B5 清除）冻结于 `docs/quality/zero-dep-baseline.json`：**只降不升**，新增即失败；A2 搬运完成后基线清零；另需**正向证据**：在未安装 akshare/openbb 的干净环境中 P0 域集成测试通过〔C51 复核，**不勾**：这条判据现在只有一个面没到——「基线清零」，而它已不是"再读一遍"能解决的面。现场 AST 走查 `import 形态 = 0`、`动态 import 形态 = 0`（`docs/evidence/C51/census-after.txt` 面 2），基线里剩下的 3 条全是**产品事实的名字面量**：`opendata/data/openbb_map.py:125` 的 `openbb` 是 FR-7 对…（完整判据见验收文档）

缺口及关闭条件：「基线清零」是这条唯一没到的面，而它不是再读一遍能解决的：现场 AST 走查 import 形态=0、动态 import 形态=0（docs/evidence/C51/census-after.txt 面 2），基线剩下的 3 条全是产品事实的名字面量 —— opendata/data/openbb_map.py:125（FR-7 对照表自己的字段名）、opendata/models/data_script.py:74（DataScript.source 的溯源默认值）、opendata/pipeline/patrol.py:468（key_status() 的数据源标签）。抹平它只有两条路，两条都要改判据本身：①把扫描器字符串规则改窄＝回头放宽已勾的 AC-16|05「AST 口径」，本轮拒绝；②改掉这三处拼写＝给扫描器做伪装，正是 C35 刚收口的那种门禁空转。**属用户/产品决策（与 task #52 同列）**。本轮做到的：「只降不升、新增即失败」两条门禁命令原样复跑 exit=0（--self-test + 默认核对，517 文件走查，逐字正文见 docs/evidence/C51/zero-dep-rerun-c51.txt，末行 ZERO_DEP_CHECK_EXIT=0）；判据后半句要求的正向证据成立，但它落在 AC-16|07 那一格，不顶替本格的清零。

证据：`docs/quality/zero-dep-baseline.json`、`docs/evidence/C51/census-before.txt`、`docs/evidence/C51/census-after.txt`、`docs/evidence/C51/zero-dep-rerun-c51.txt`、`docs/evidence/C51/README.md`、`docs/evidence/C36/zero-dep-surface-after.txt`

### §4|02

判据：**口径映射表复核**：字段映射/单位换算/key 规范化经人工确认（抽样比对原始响应）

缺口及关闭条件：缺具名人工原始样本字段、单位与业务 key 抽样确认。需签认映射；复权扩展口径已由用户确认，见决定档案。

证据：`opendata/data/mappings/akshare.yaml`、`opendata/data/mappings/ths.yaml`、`docs/evidence/C64/acceptance-decisions.md`

### §4|04

判据：搬运保真对照报告（AC-6）与 fuyao 对照（与官方 Parquet/文档示例）归档

缺口及关闭条件：A2 回放仍含待判案例；A3 dump 导入行数不证明官方 Parquet/样例字段与值对照。需覆盖、字段/类型、值差异和容差报告。

证据：`docs/evidence/A2/compare-report.md`、`docs/evidence/A3/fuyao-dump-import.txt`

### §5|01

判据：**写入基准**：单源日线全量落库耗时与内存记录在案（内存恒定，不随数据量线性增长）

缺口及关闭条件：缺全量写入规模、峰值 RSS 及随规模变化的内存测量，不能由有界夹具推出恒定内存。与 AC-8|04 共享一次合规测量。

证据：`docs/evidence/A4/write-benchmark.txt`、`docs/evidence/C58/README.md`

### §5|02

判据：全市场 10 年日线回补任务：断点续跑完成，进度表可观测（不设硬性时长）

缺口及关闭条件：十年 THS 行数不证明全市场十年任务中断、续跑且完成。需隔离数仓全市场十年任务、已完成分片跳过和最终覆盖记录。

证据：`docs/evidence/B1/README.md`、`docs/evidence/A3/fuyao-dump-import.txt`
