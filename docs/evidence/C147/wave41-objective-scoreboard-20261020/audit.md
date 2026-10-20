# 迭代 2 目标完成审计（2026-10-20，wave 41）

本页按目标原文逐条建清单，把每条要求映射到可重跑的载体与判定。**结论：未完成**——五条里两条满足、
一条已违反、两条被外部条件卡住。判定脚本 `probes/w105_objective_scoreboard.py`
（`objective_complete=False reasons=['residue_owner_gated=11','providers_without_rows=3','subagent_over_cap=4']`）。

| # | 目标要求（原文） | 实测 | 载体 / 复算命令 | 判定 |
| --- | --- | --- | --- | --- |
| 1 | 32 个 OpenBB provider 的**本仓自研实现** | **25 / 32 全部行已 accepted**；4 个部分（econdb 6/8、intrinio 37/38、seeking_alpha 1/3、yfinance 28/29）；3 个无行（biztoc / stockgrid / wsj）；31 个 provider 包在场 | `w72`/`w112`（`providers_with_at_least_one_accepted_row=29`、`zero=[...]`、`provider_packages=31`）；inventory CSV 350 行、32 provider | **未达成**（3 个 provider 属 owner 闸门：凭证 / 端点+权利 / 本机传输） |
| 2 | clean-room，OpenBB 仅作输出契约对照 | 445 个在范围模块 AST 解析、**0** 个 `openbb*` import 根、0 个绝对路径引用；325 个 vendor 文件显式排除；注入 import 的反例臂可咬 | `w73`/`w112`（`openbb_import_violations=0`、`in_scope_files=445`）；`w132`/`w133`（6 行同文窗口：逻辑 0、表 260，更严 tokenizer 下仍全字面量） | **满足** |
| 3 | 逐条验收，且以可复算证据面（判定探针 + counterfact 反例）落档 | 339 行 × 5 相 receipt；0 行失效 pin、0 行缺相、0 个无拒绝令牌的 counterfact、0 处摘要不一致；**339 个 counterfact 的 observed 码 == expected 码 == 见证声明**（0 不一致、0 非零退出）；**1,695 份五相 receipt 全部 PASS / exit 0，且命令与 task_id 指向自己那一行与相位**；11 行 residue 每行有实测闸门（4 凭证 / 5 已测无答复 / 2 未测） | `w75`/`w111`（交付树上重跑）；`w130`（反例码三等式，6/6）；`w67`/`w113`；`w105`（现场重跑判定器：350/350 状态一致、0 缺失、身份集不变，A–L 读数不变；claim↔row 映射：339×5 PASS + 6×5 BLOCKED + 5×0 = 1725，forged 可咬；claim→receipt 指针：1,695 PASS 全命中且摘要/任务/相位/exit 一致、30 BLOCKED 为空指针+理由）；`face.md` §7–§9、§21、§26、§32、§34 | **机制满足、人口未满**：339/350 行有逐条证据；残差 11 行按 owner 裁定挂待办，不计入已验收 |
| 4 | 禁止口头判定 | 每个数字都带 arm；本轮另修两处自家仪器缺陷（`w99` rc、argv 泄漏）并留红档 | 各载体 `arms=N arms_passed=N PASS=True`；C147 `face.md` §3 | **满足** |
| 5 | 子代理总数 ≤ 30 | 实测 **34**（逐 transcript 去重，`w74`：`cap=30 this_thread_total=34 over_by=4`） | `w74`（7/7 arms） | **已违反**，无法回溯修复，只能留档；本 goal 不再派单 |

## 更新记录（2026-10-20，wave 41y/41cc）

* `w105` 现在**现场重跑**仓内判定器（输入为台账 CSV 与 `claims.merged.json`），再与已入档报告逐行比对：
  350/350 状态一致、0 缺失、0 多余、身份集不变；深拷贝里改一个状态后比较器报 1 处不同（`face.md` §26）。
* DEC-41-05 以仓内 §6.2 原文（迭代 1 `需求文档.md`「数据域优先级」，P0/P1/P2 三档）收口：
  `test_scheduled_patrol` 的陈旧两档断言修正，测试平面 **11 → 10**（`w41i`，`new=0`，仅少那一条；
  `face.md` §28）。
* 两项都不改变本页结论：**未完成**——残差 11 行、3 个 provider 无行、子代理 34 > 30。

## 未达成项各自缺什么

* **provider 1/2/3（biztoc / stockgrid / wsj）**：分别缺凭证授权、端点与权利登记、可达的本机传输。
  计划 §7 DEC-39-04 / DEC-39-05 / DEC-39-06 给出成本与默认动作。
* **残差 11 行**：与上条同源；另 2 行（stockgrid、yfinance::AvailableIndices）分别无端点记录与 0 endpoints
  （后者若要落地需为「无 HTTP 请求的本地常量」建 reference-input 见证，改 runner ⇒ 全树重签，DEC-40-02）。
  seeking_alpha ×2 的空信封与参数无关：15 次参数尝试里，参数齐全的 200 响应对假 ticker
  （`ZZZZNOTREAL`）、数字 id、假 item 都返回同一个 31 字节 `{"revisions":{},"estimates":{}}`
  （C144 `w54` D 臂 distinct=1），所以继续补参数不会产生数据；wsj ×3 是本机传输不可达
  （3 次 timeout、0 字节 header）。
* **子代理 34 > 30**：已发生的越界，不能通过任何后续动作变回「未发生」。

## 如果 owner 做出不同裁定，判定会怎么变

| 裁定 | 变化 |
| --- | --- |
| DEC-39-04 提供 4 行凭证 | 最多 +4 accepted rows（339 → 343），provider 覆盖 29 → 30 |
| DEC-39-05 给 stockgrid 端点与权利 | +1 行、provider 30 → 31（与上条叠加顺序无关） |
| DEC-39-06 换可出网环境 | wsj 3 行可重测，最多 +3 |
| DEC-40-02 建 reference-input 见证 | AvailableIndices 1 行可评；代价是全树重签 45–60 分钟 |
| DEC-41-02 扩 350 身份集 | 分母本身改变，其余口径需重算 |

以上任何一条都不会改变第 5 条的违反结论。
