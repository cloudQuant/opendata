# 迭代 2 目标完成审计（2026-10-20，wave 41）

本页按目标原文逐条建清单，把每条要求映射到可重跑的载体与判定。**结论：未完成**——五条里两条满足、
一条已违反、两条被外部条件卡住。判定脚本 `probes/w105_objective_scoreboard.py`
（`objective_complete=False reasons=['residue_owner_gated=3','providers_without_rows=1','subagent_over_cap=4']`）。

| # | 目标要求（原文） | 实测 | 载体 / 复算命令 | 判定 |
| --- | --- | --- | --- | --- |
| 1 | 32 个 OpenBB provider 的**本仓自研实现** | **30 / 32 全部行已 accepted**；1 个部分（seeking_alpha 1/3）；1 个无行（stockgrid）；intrinio 38/38（wave46 OptionsSnapshots 落地）、econdb 8/8、wsj 3/3、biztoc 1/1、yfinance 29/29 已落地；32 个 provider 包在场 | `w105` 现场读数（`providers_with_rows=31`、`zero_providers=[stockgrid]`、`provider_split={full:30, partial:[seeking_alpha 1/3], none:[stockgrid]}`）；`w73` `provider_packages=31`；inventory CSV 350 行、32 provider | **未达成**（仅 stockgrid 属 owner 闸门：端点+权利；wsj/biztoc 已按 REFERENCE-EXCEPTION-01 落地） |
| 2 | clean-room，OpenBB 仅作输出契约对照 | 445 个在范围模块 AST 解析、**0** 个 `openbb*` import 根、0 个绝对路径引用；325 个 vendor 文件显式排除；注入 import 的反例臂可咬 | `w73`/`w112`（`openbb_import_violations=0`、`in_scope_files=445`）；`w132`/`w133`（6 行同文窗口：逻辑 0、表 260，更严 tokenizer 下仍全字面量） | **满足** |
| 3 | 逐条验收，且以可复算证据面（判定探针 + counterfact 反例）落档 | 347 行 × 5 相 receipt；0 行失效 pin、0 行缺相、0 个无拒绝令牌的 counterfact、0 处摘要不一致；**347 个 counterfact 的 observed 码 == expected 码 == 见证声明**（0 不一致、0 非零退出）；**1,735 份五相 receipt 全部 PASS / exit 0，且命令与 task_id 指向自己那一行与相位**；3 行 residue 每行有实测闸门（2 INCOMPLETE + 1 UNREVIEWED） | `w75`/`w111`（交付树上重跑，4/4 arms）；`w130`（反例码三等式，6/6）；`w67`/`w113`；`w105`（现场重跑判定器：350/350 状态一致、0 缺失、身份集不变，A–L 读数不变；claim↔row 映射：347×5 PASS + 2×5 BLOCKED + 1×0 = 1745，forged 可咬；claim→receipt 指针：1,735 PASS 全命中且摘要/任务/相位/exit 一致、10 BLOCKED 为空指针+理由；counterfact 对照语句 347/347，均为 runner 输出中的声明而非独立 artifact）；`face.md` §7–§9、§21、§26、§32、§34、§36、§45–§46 | **机制满足、人口未满**：347/350 行有逐条证据；残差 3 行按 owner 裁定挂待办，不计入已验收 |
| 4 | 禁止口头判定 | 每个数字都带 arm；本轮另修两处自家仪器缺陷（`w99` rc、argv 泄漏）并留红档 | 各载体 `arms=N arms_passed=N PASS=True`；C147 `face.md` §3 | **满足** |
| 5 | 子代理总数 ≤ 30 | 实测 **34**（逐 transcript 去重，`w74`：`cap=30 this_thread_total=34 over_by=4`） | `w74`（7/7 arms） | **已违反**，无法回溯修复，只能留档；本 goal 不再派单 |

## 更新记录（2026-10-20，wave 41y/41cc）

* `w105` 现在**现场重跑**仓内判定器（输入为台账 CSV 与 `claims.merged.json`），再与已入档报告逐行比对：
  350/350 状态一致、0 缺失、0 多余、身份集不变；深拷贝里改一个状态后比较器报 1 处不同（`face.md` §26）。
* DEC-41-05 以仓内 §6.2 原文（迭代 1 `需求文档.md`「数据域优先级」，P0/P1/P2 三档）收口：
  `test_scheduled_patrol` 的陈旧两档断言修正，测试平面 **11 → 10**（`w41i`，`new=0`，仅少那一条；
  `face.md` §28）。
* 两项都不改变本页结论：**未完成**——残差 11 行、3 个 provider 无行、子代理 34 > 30（wave42 前读数；
  之后为残差 8、无行 2，见下条）。
* 2026-10-21 wave42：WSJ 3 行按 REFERENCE-EXCEPTION-01 落地（声明式 engine + 浏览器 UA），
  五相 15/15 PASS；新增 3 domain 触发全树重签（342 reviews），判定器 **342/350**、claims 1740；
  测试平面新增 9 红全部修掉（3 条改为按契约必需列跳过），残差降为 8、无行 provider 降为 2。
* `w105` 另补三条行级不变量（`face.md` §32、§34、§36；wave42 前读数）：claim↔row（339×5 PASS + 6×5
  BLOCKED + 5×0 = 1725）、claim→receipt 指针（1,695 条全部命中且摘要/任务/相位/exit 一致，
  30 条 BLOCKED 为空指针+理由）、counterfact 对照语句（339/339 行，属 runner 输出中的声明，
  非独立 artifact）。每条都有深拷贝伪造臂；记分板现为 **21/21 arms PASS**。
* 2026-10-21 wave44：econdb ExportDestinations 与 yfinance AvailableIndices 落地（343→345），判定器
  345/350、claims 1745；平面 `test_plane_w44b_final.xml` 10 红（同集合），collected 26,763。
* 2026-10-21 wave45：econdb CountryProfile 用公开 `series/context` 路由落地（345→346，econdb 8/8）；
  第四次全树重签 31 provider（0 producer/recorder fail；intrinio refused=1、seeking_alpha refused=2
  均为既有残差）；判定器 **346/350**、claims 1745（346×5 PASS + 3×5 BLOCKED）、reviews 346；平面
  `test_plane_w45_final.xml` **10 failed, 26,452 passed, 305 skipped**（同集合，collected 26,767 =
  26,634 + 143 − 10）；`w105` **21/21**（accepted 346、residue 4、plane_new=0）；`w111` 4/4、
  `w130` 6/6、`w113` 4/4。
* 2026-10-21 wave46：intrinio OptionsSnapshots 以手写两次请求 fetcher 落地（346→347，intrinio 38/38，
  `NOT_DECLARABLE` 收空）；第五次全树重签 31 provider（0 fail）+ 两次 intrinio 局部重签（pin 后编辑）；
  判定器 **347/350**、claims 1745（347×5 PASS + 2×5 BLOCKED）、reviews 347；残差 3。

## 未达成项各自缺什么

* **provider stockgrid**：缺新端点与权利登记（旧端点 301→axlfi）。wsj（DEC-39-06）、biztoc（DEC-39-04 的离线部分）与 econdb CountryProfile（wave45）已关闭。
* **残差 3 行（wave46 后，逐行归因）**：stockgrid::ShortVolume（1，旧端点 301→axlfi.com，不再供数，
  UNREVIEWED）；seeking_alpha×2（2，空信封，PerimeterX captcha 挡住 ID 解析，无法靠公开 GET 取数，
  INCOMPLETE）。intrinio::OptionsSnapshots 已于 wave46 以两次请求的手写 fetcher 落地（38/38）。
* **子代理 34 > 30**：已发生的越界，不能通过任何后续动作变回「未发生」。

## 如果 owner 做出不同裁定，判定会怎么变

| 裁定 | 变化 |
| --- | --- |
| ~~DEC-39-04~~ biztoc | 已离线落地（343/350）；key 只服务 live，**0 accepted rows** 受 key 影响 |
| 为 econdb / intrinio 提供 key | **0 行**：两 provider 均已无残差（econdb 8/8、intrinio 38/38），key 只服务 live 复核 |
| DEC-39-05 给 stockgrid 新端点与权利 | +1 行（347→348）、provider_with_rows 31 → 32（需 AXLFI 新接口；旧端点已退役） |
| ~~DEC-39-06~~ 已关闭 | wsj 3 行已落地（342/350），不再计入待裁定 |
| ~~DEC-40-02~~ 已关闭 | yfinance AvailableIndices 经伪 transport 的 reference-input 见证落地（343→345 同波 +2），无需改 runner |
| DEC-41-02 扩 350 身份集 | 分母本身改变，其余口径需重算 |

以上任何一条都不会改变第 5 条的违反结论。
