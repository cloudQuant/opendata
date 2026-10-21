# 迭代 2 目标完成审计（2026-10-20，wave 41）

本页按目标原文逐条建清单，把每条要求映射到可重跑的载体与判定。**结论：未完成**——五条里两条满足、
一条已违反、两条被外部条件卡住。判定脚本 `probes/w105_objective_scoreboard.py`
（`objective_complete=False reasons=['residue_owner_gated=1','providers_without_rows=1','subagent_over_cap=4']`）。

| # | 目标要求（原文） | 实测 | 载体 / 复算命令 | 判定 |
| --- | --- | --- | --- | --- |
| 1 | 32 个 OpenBB provider 的**本仓自研实现** | **31 / 32 全部行已 accepted**；0 个部分；1 个无行（stockgrid）；seeking_alpha 3/3（wave55 两份 estimates 落地）、intrinio 38/38（wave46）、econdb 8/8、wsj 3/3、biztoc 1/1、yfinance 29/29 已落地；32 个 provider 包在场 | `w105` 现场读数（`providers_with_rows=31`、`zero_providers=[stockgrid]`、`provider_split={full:31, partial:[], none:[stockgrid]}`）；`w73` `provider_packages=31`；inventory CSV 350 行、32 provider | **未达成**（31/32，仅 stockgrid 属 owner 闸门：端点+权利；wsj/biztoc 已按 REFERENCE-EXCEPTION-01 落地） |
| 2 | clean-room，OpenBB 仅作输出契约对照 | wave41 代际 445 个在范围模块 AST 解析、**0** 个 `openbb*` import 根（本轮 `w73` 读数 `provider_packages=31`、`openbb_import_violations=0`）、0 个绝对路径引用；325 个 vendor 文件显式排除；注入 import 的反例臂可咬 | `w73`/`w112`（`openbb_import_violations=0`、`in_scope_files=445`）；`w132`/`w133`（6 行同文窗口：逻辑 0、表 **293**，21 个本仓文件；更严 tokenizer 下 293/293 仍全字面量，`face.md` §47） | **满足** |
| 3 | 逐条验收，且以可复算证据面（判定探针 + counterfact 反例）落档 | 349 行 × 5 相 receipt；0 行失效 pin、0 行缺相、0 个无拒绝令牌的 counterfact、0 处摘要不一致；**349 个 counterfact 的 observed 码 == expected 码 == 见证声明**（0 不一致、0 非零退出）；**1,745 份五相 receipt 全部 PASS / exit 0，且命令与 task_id 指向自己那一行与相位**；1 行 residue 有实测闸门（**0 INCOMPLETE + 1 UNREVIEWED**） | `w75`/`w111`（交付树上重跑，4/4 arms）；`w130`（反例码三等式，6/6）；`w67`/`w113`；`w105`（现场重跑判定器：350/350 状态一致、0 缺失、身份集不变，A–L 读数不变；claim↔row 映射：349×5 PASS + 0 BLOCKED + 1×0 = 1745，forged 可咬；claim→receipt 指针：1,745 PASS 全命中且摘要/任务/相位/exit 一致（`receipt_missing=0`、`blocked=0`）；counterfact 对照语句 349/349，均为 runner 输出中的声明而非独立 artifact）；`face.md` §7–§9、§21、§26、§32、§34–§36、§45–§58 | **机制满足、人口未满**：349/350 行有逐条证据；残差 1 行（stockgrid）按 owner 裁定挂待办，不计入已验收 |
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
* 2026-10-21 wave47/48（复测与修红）：clean-room 两面复测 logic 仍 **0**、table **260 → 293**，+33 逐文件
  归因为 econdb `profiles.py` 19 + intrinio `options_snapshots.py` 14（`face.md` §47，`w132` 5/5、
  `w133` 8/8 后者分母重钉）；§46 披露的 A2 mypy 既存红（econdb 7 条）已修到 **0** 并局部重签，
  判定器不变 **347/350**；修后平面 `test_plane_w46b_final.xml` 仍 10 红同集合（26,455 passed、
  collected 26,771），`w105` 21/21，收口 `30faded84` 8/8 arms（首跑 drift 1 是固定名闸门载体的自指，
  见 `face.md` §48）。
* 2026-10-21 wave49：整树 `Fetcher` 错型普查落地（`w148`，394 模块 / 单进程 mypy /
  谓词 `override`+`no-any-return` / git 双侧对照 `pre_fix=5`、`post_fix=0`，**6/6 arms**）。
  首跑点名 2 个文件、4 条错误（biztoc `world_news.py`、yfinance `available_indices.py`，
  均为 wave43/44 落地件），已按 §48 同法改 `TRaw` 并补掉两笔 A2 顶出的既存小债（E501、D102）；
  复跑 `defect_class_files=0`。两次局部重签后判定器不变 **347/350**、`w105` 21/21
  （`face.md` §49）。同日按新鲜度棘轮重取：平面
  `test_plane_w49_final.xml` 仍 **10 红**且失败集合与 `w46b` 按 id 逐条相同（`new=[] fixed=[]`），
  `w111` 4/4、`w113` 4/4、`w130` 6/6。
* 2026-10-22 wave50：残差 3 行的阻塞理由按新鲜度棘轮重测（`w150_blocked_retry_face`，**5/5 arms**，含 dead-host 反例与可翻转的 verdict 谓词）。SA 今天：search **200/`{}`**、estimates **404 HTML×5 逐字节相同**；stockgrid：重定向后 **200 / 1.26 MB 落地页 HTML / 非 JSON**。结论不变（`rows_advanceable_now=0`），DEC-39-04/05 措辞已按今日读数改写（`face.md` §51）。
* 2026-10-22 wave51：**§51 的 SA 读数作废并已重测**——`w150` 首版用的 estimates base path 与参数形状都不在仓内记录里（记录是 `api/v3/symbol_data/estimates` + `ticker_ids[]=…`），所以那不是复核。修正后的当日实测：SA 7 个请求**全部 403 / 413 B 的 PerimeterX 挑战体**（`blockScript`/`captcha`，answered=0），stockgrid 仍是 1.26 MB 落地页 HTML；谓词一度把「挑战 body 各不相同」误读成解锁，已补 `_px_challenge` 与伪造对照，`w150` **5/5 arms**、`rows_advanceable_now=0`。残差 3 行的结论不变（`face.md` §52）。

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
| DEC-39-05 给 stockgrid 新端点与权利 | +1 行（349→**350/350**）、provider_with_rows 31 → 32（需 AXLFI 新接口；旧端点已退役） |
| ~~DEC-39-06~~ 已关闭 | wsj 3 行已落地（342/350），不再计入待裁定 |
| ~~DEC-40-02~~ 已关闭 | yfinance AvailableIndices 经伪 transport 的 reference-input 见证落地（343→345 同波 +2），无需改 runner |
| DEC-41-02 扩 350 身份集 | 分母本身改变，其余口径需重算 |

以上任何一条都不会改变第 5 条的违反结论。
* 2026-10-21 wave55/56/58：`seeking_alpha::Forward{Eps,Sales}Estimates` 落地（347→**349/350**，
  首次 **0 行 INCOMPLETE**）。第六次全树重签 + 两次局部补跑；`w105` 21/21（accepted 349、residue 1、
  split full 31 / partial [] / none [stockgrid]）、`w111` 4/4、`w130` 6/6、`w113` 4/4；平面
  `test_plane_w55_final.xml` 仍 10 红同集合（collected 26,779 = 26,634 + 161 − 16）。同波补跑 A2 质量面，
  清掉落地提交漏下的 4 条 ruff 红（`face.md` §55–§58）。
