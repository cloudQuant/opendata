# C65 搬运范围扩张棘轮审查

日期：2026-10-01；主审：Codex 主智能体。`ported-scope-ratchet-review.json` 由只读独立盘点生成，旧快照原字节保留 `ratchet-before-futures-derivative-scope.json`。

FR-4/D9明确要求的 futures_derivative 增加12个冻结MIT源码文件以及平面API的13个必要导出。固定Ruff0.15.20在HEAD快照实测E/F2144、直连HTTP1044；当前325py为2188/1071。新增文件贡献21个E501与27个HTTP调用，旧范围facade贡献10个E402和13个F401。其余旧路径无增长；不能写“旧313文件债务未增加”。

主线程逐段查看facade差异，13个符号全部来自新增搬运组；保持既有上游导出形式，没有修改业务实现来消除搬运保真债务。新增12文件SHA均与manifest、lock和固定上游一致。依据CODE_QUALITY.md §7，批准这一明确B层范围扩张的受控force-update，前提是A2零违例、首方Ruff≤148/mypy=0/Bandit≤1、扫描版本保持。未批准首方债务上调、静默缩小扫描范围或其他未登记增长。

`ratchet-scope-review-decision.json` 留存决定和条件；实际update/check命令及exit另行留档。安全1048条发现及31条HIGH仍保留，与棘轮范围冻结分开判定。
