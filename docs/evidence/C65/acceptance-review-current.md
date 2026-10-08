# C65 当前审阅与验收边界

日期：2026-10-01（Europe/Madrid，原始日志保留实际 UTC 时间）。基线 HEAD：`d552c081950edffce167e3dfc0be74f37a6950a1`；本轮工作区未提交。主审：Codex 主智能体。用户直接授权“你帮我直接审阅”。最终全量门禁与条目汇总仍在执行。

## 已执行的当前审阅

- **数据权利**：`docs/data-rights-registry.md` 的11条来源逐项登记公开条款、用途边界、复核日期和责任人。免费访问、公开SDK与免费Key不自动覆盖落库、批量获取或再分发；限制、未知和不适用项保持原结论。登记表完成不等于全部用途获得授权。
- **provider 当前源码**：7包71py逐文件SHA与固定OpenBB五包121py的源码对照、AST筛选和最高三对实际源码抽查，见 `provider-source-review.json`、`provider-similarity-inventory.json`、`provider-nearest-review.json`。当前比较范围未观察到复杂实现复制；历史开发声明和全库来源断言仍有缺口。
- **字段与业务键**：33条启用能力逐项审阅，见 `mapping-review-results.json`。THS官方股票日线样本限定通过；公司行动3组重复键、2组值冲突，以及财报公告/修订时点和部分单位语义未闭合，整体映射审阅为 INCOMPLETE。
- **搬运安全**：Bandit1.9.4扫描325py得1048条、15规则、31条HIGH、0扫描错误；全部finding身份绑定规则处置和跟进。`ported-security-triage.json` 明确保留TLS、远端eval和pickle等风险，不能将审阅完成写成漏洞已修复。
- **旧库逐表审查**：`legacy-per-table-review.csv` 覆盖1026张旧表的结构、业务键与迁移映射状态；1张明确映射、1025张未映射并保留。唯一已映射表617371行的真实隔离迁移因必填“成交额”为空而失败：616694行NULL，目标写入0，见 `legacy-field-quality-census.json` 和 `legacy-transfer-current.json`。未给缺值填0、未删除旧表，生产3306写入0。

## 实际开发与运行验证

Docker镜像补齐SQLAlchemy asyncio所需greenlet依赖；Vite生产包移除导致初始化循环的手工chunk分组。当前隔离33565数据库与33566应用：后端健康、默认双库名、真实注册/前端登录、首页和目录认证状态通过，见 `runtime-stack-current.json` 及独立driver。前端回归只作用于本地隔离应用。

当前wheel在两个独立干净环境安装，`pip check`通过，上游akshare/openbb/openbb_platform均不存在，P0集成各211 passed。`clean-env-integration-run.txt` 保留具体模块、安装位置、命令和exit；这是P0集成证据，不替代整仓门禁。

全量ODS写入基准完成100k、1m和10,310,289行三个规模的耗时与峰值RSS测量。十年断点恢复已在隔离schema完成：窗口2016-09-24..2026-09-24、5568标的、10,307,812行，实际中断exit75后恢复exit0并跳过已完成2shards，223shards与3hooks全部完成。独立SELECT全键与10个ODS原始字段/6个DWD归一化字段对照，缺键及值差异均0，进度累计行数与两个目标表完全相等，见 `replay-full-independent-sql.json` 和对应driver。恢复耗时3379.259278秒，峰值RSS546914304字节。来源是截至9/24的既有ODS快照，不能声明最新供应商取数或当前全市场完整性；§5|02保持gap。执行时冻结工具SHA及另外13个运行时文件均核对一致；后来唯一局部类型标注变更另有字节/AST等价档案。

## 保留的验收门槛

完整质量门禁、当前条目探针、台账与勾选对账须在实现冻结后重新执行。正式库分区和跨年写入、真实双源/四域取数、官方复权序列对照、交易日跑批、生产备份RPO与通知送达等缺口按实际现场证据判定，隔离预演不能替代。用户要求完成逐表迁移审查后再决定删除，当前DROP仍未授权。
