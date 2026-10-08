# C65 搬运范围与抽样验收决定

日期：2026-09-30；审核者：Codex 主线程。

## 原判据与抽样单位

AC-6|02 的原文为「迭代1B：其余子模块抽样≥20%对照通过」。实施计划B1.1逐个列出 `stock_feature/stock_fundamental/futures/index/fund/option/bond/economic` 八个目录，B1.3使用「其余域抽样≥20%」。据这两处原始口径，分母为列出的子模块组；加上FR-4/D9明确要求的 `futures_derivative` 后为9组。没有修改验收文字、阈值、P0/P1优先级或把未启用目录加入分母。目录下245个Python文件与1个资源逐文件库存仍完整披露；文件级抽样7/245=2.86%，本轮不声称达成文件级20%。

## 本轮实际对照

主线程在锁定上游commit及当前搬运树，运行既有离线record/replay仪器（只重定向输出文件，算法/样本/容忍度均未改）。14个已录制案例全部PASS，分别覆盖8个源文件、10个public函数；其中1B分母中的6组/9组通过，覆盖7文件、9函数，覆盖率66.67%≥20%，最低需要ceil(9×20%)=2组。6组为 stock_fundamental/index/futures/option/bond/fund；容忍度float rtol=1e-9，列/形状/dtype及非浮点逐格相等；全部文本不同而浮点相等计数为0。

完整报告仍以exit=1结束，因为4个东财录制案例为PENDING（不可达/空返回）；这些案例计入未判读清单，未计为PASS或分子，亦未用其他数据源冒充。这不改变其余子模块6/9的条目级结论。其余组stock_feature/economic/futures_derivative未覆盖，符合本条抽样门槛，不能被表述为所有子模块/所有provider通过。D10新浪宽窗118天PASS，默认东财真机qfq/hfq另有10个HTTP502，AC-11|02仍gap。

## 搬运范围

`port-scope-manifest.json`/`port-scope-review.md`逐路径连接327项上游锁、manifest、当前目标SHA和原报告：325Python+2资源，9处明确人工适配，源SHA全部与锁定checkout一致。主线程另实际生成 `port-report-current.md`：327项逐项codemod重放，0个TODO。因此AC-5|02完成；AC-5|03的AST依赖与安全triage另行验收。

证据：`port-fidelity-current.txt`、`port-fidelity-current-report.md`、`port-report-current.txt`、`port-report-current.md`、`port-scope-manifest.json`。
