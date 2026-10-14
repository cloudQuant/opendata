# 历史元数据流程说明暴露记录

记录日期：2026-10-12。此记录只描述暴露范围，不复制上游算法内容。

主代理在读取中央注册快照的 missing/binding 统计时，以 `rg` 检索 `missing|finviz|tmx|TreasuryPrices`。快照 `integration/runs/central-wiring-20261012T105243Z/run.json` 同时保存了历史 `capability_notes`；检索输出意外展示其中关于 Federal Reserve TreasuryRates、SvenssonYieldCurve 及 Tiingo CryptoHistorical 的 OpenBB 实现流程说明。主代理没有打开 OpenBB 源码，但这些历史说明超出本轮允许的公开 query/output 契约范围。

主代理立即要求后续快照摘要只输出结构化身份、domain、状态、路径和哈希，排除自由文本 `capability_notes`。旧快照和原始证据不改写。主代理不使用所见实现说明制定这些来源的取数或转换算法；已有设计决定以暴露前的官方资料/公开契约审查记录为界，后续实现另由未暴露工作者依据独立资料完成。

原 Fed 实施者随后确认，此前读取本仓旧 specs/model 注释时已看到上游流程描述；该实施者尚未开始最新五项字段修复，现停止这部分实施。原 Fed 验收者也确认读取过该 source/spec 层，原 Fed13 收据保留为历史行为证据，不算 clean-room 独立验收。替代工作者 `impl_congress_cboe` 明确确认未读过上述内容，接手全新文件中的五项独立实现，只允许官方原始数据和公开 query/output 文档；其余 Fed 切片的来源复核仍未关闭。

Tiingo/Econdb/Benzinga 实施者声明未读取或依赖历史 capability notes，也未查看上游提取/转换实现；其声明与独立资料路径存于 `tiingo-econdb-benzinga/provenance-addendum.md`。声明本身不等于独立 clean-room 验收，旧 Tiingo 声明式能力仍须分别核对其来源链。

这是本轮来源链缺口，不用“没有复制代码”替代处理。没有删除历史证据，没有将暴露后的历史结论升级为独立验收 PASS。
