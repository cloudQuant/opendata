# Wave 41 —— 迭代 2 的目标记分板（一页可重算）

这一页不产出新功能，它把目标的每一条口径压到一张可重跑的表上：**能重算的都重算**（判定报告、merged claims、
任务台账 CSV、两份 junit 平面），只能引用的则钉到载体并连同其 `PASS=True` 行一起引用。判定脚本是
`probes/w105_objective_scoreboard.py`，载体 `runs/w105_objective_scoreboard.txt`。

## 记分板（2026-10-20 读数）

| 口径 | 值 | 来源 / 重算方式 |
| --- | --- | --- |
| 平面 1 accepted | **339** | 判定报告逐行计数，且与 `w57` 载体的 `after_accepted` 相等 |
| 身份总数 | **350** | 判定报告 `identity_count` == 台账 CSV 行数 == `w57` 的 `identities` |
| 身份集 | **5168e1e723999312** | 判定报告；与 `w57` 记录一致，重签未移动 |
| claims 总数 | **1725** | `claims.merged.json` 长度，且与 `w57` 的 `after_claims` 相等 |
| 有 ≥1 accepted 行的 provider | **29 / 32** | 判定报告重算，且与 `w72` 载体相等；零行集合 `{biztoc, stockgrid, wsj}` |
| 残差 | **11** | `identities - accepted`，且与 `w67` 的 `sums=11` 相等 |
| 逐行验收 | **339 行、0 行失效 pin** | `w75`（10/10 arms）——逐行核 receipt 摘要与反例文本 |
| clean-room | **0 个 OpenBB import / 445 模块** | `w73`（6/6 arms，含注入 import 的反例臂） |
| AC2-10 `dev_done` | **17 == engine_declared_rows** | `w70`，即该谓词自身天花板（owner 裁定见 C145 §15） |
| query-schema 分叉 | **114 行 / 740 pin 站点 / 9 provider** | `w95`（7/7 arms） |
| 测试平面 | **27 failed（wave 39 的 28 条里修好 1 条，new=0）** | 两份 junit 经 `w51` reader 取集合；见 §2 |
| 子代理 | **34 次 / 上限 30** | `w74`（7/7 arms），超 4 次记为违反 |

## 2. 修掉一条 pre-existing 红：immutable-evidence 围栏整整落后了一个分类世代

`tests/test_immutable_evidence.py` 原先断言 `snapshot_paths == 172`、`excluded_paths == 172`、
`historical_ruff_count == 3974`、`active_entrypoints == 18`。而 `scripts/quality/immutable_evidence.py`
钉住的分类件路径字面就是 `immutable-wave9-exact5055/classification.json`，其 SHA-256 等于模块里的
`CLASSIFICATION_SHA256`（`w109` 的 A 臂）。也就是说：**源真值是那个被代码钉住 sha 的分类件，而测试的
四个字面量还停在上一代 exact172**。本次把测试对齐到钉住的分类件，不是放宽判定——同一文件仍要求
`errors == ()`、仍逐字面量相等，分类件一变就还会红。

实测（`runs/w109_immutable_fence_rebase.txt`，7/7 arms）：

```
artifact_sha_matches_pin=True   validation_errors=0 blocked=0
live={snapshot_paths: 5055, excluded_paths: 5055, ruff: 149323, entrypoints: 343}
partition tools=343 excluded=5055 tools_are_entrypoints=True
pytest exit=0  summary=18 passed in 79.32s
```

四个数字不是从记忆里填的：`w109` 从 `validate_manifest()` 读出 live 值，再把测试源码里的字面量
正则读回来逐项比较（D 臂），并伪造一条 +1 证明比较能失败（E 臂）；分区臂证明 9 个命名入口点仍留在
工具集、没有落进 5055 的排除集（F 臂）。

对测试平面的影响（`runs/test_plane_w41b_final.txt` + 重跑后的 `w105`）：

```
wave 39 final = 28 failed / 26335 passed / 271 skipped
wave 41b      = 27 failed / 26336 passed / 271 skipped   collected 同为 26634
集合：shared=27  new=0  fixed=1      fixed 的正是这一条断言
```

`w105` 的 I 臂因此改成集合算术（`shared+fixed==before`、`shared+new==after`、`new==0`），既允许
"修好一条"，又仍然禁止任何新增红。

## 3. 提交口径更正（记录，不改写历史）

`e3612db5b` 的标签是 `fix(tests)`，但它实际携带 **11 个文件**——测试文件外加 wave 41b 的全部证据
（w109/w105/w108、平面 junit、载体）；`d06a7f262` 的标签是 docs(evidence)，却只带了一个 carrier 的
增量。原因很直白：我把测试与证据一起 stage 了一次，随后 `git commit` 未限定路径，于是两批一起进了
第一个提交。内容无损、后续提交也把它们都签进了历史，但**标签与载荷不一致**这件事按证据纪律记在这里，
而不是用 amend 改写一条尚未推送之外就看不到的痕迹。

下次同类提交的口径：先只 stage 测试/产品文件并提交，再 stage 证据并提交；**`git commit` 不带路径时提交
的是整个已暂存集**，所以"只提交这一个文件"必须靠分批 stage 或显式路径来实现。凭证面覆盖的是这两批的
**并集**（11 文件、11/11 arms），所以扫描范围没有缺口。

## 4. 第二条 pre-existing 红：CongressAmendments 的 expected 夹具停在字符串世代

`tests/test_congress_cboe_iter2.py` 的 CongressAmendments 期望夹具写 `"number": "42"`（字符串），
而契约模型的字段是 `StrictInt`（`opendata/data/models/congress_cboe_iter2.py`），适配器在映射时已经
`_int_or_none(_first(row, "amendmentNumber", "number"))` 转成整数（`congress_gov/models/iter2.py:1436`）。
测试自己对 `expected` 调 `contract.model_validate(...)`，所以夹具里的字符串直接被 pydantic 拒掉：

```
E  number  Input should be a valid integer [type=int_type, input_value='42', input_type=str]
```

**产品侧没有改**（模型和适配器都不动，二者本来就是对的），只把夹具改为 `42`。读数：该文件
`1 failed / 33 passed` → **34 passed**；全量平面 `27 → 26 failed`，26337 passed，collected 不变。
与 §2 合起来，wave 39 final 的 28 条红里已 `fixed=2`、`shared=26`、`new=0`（`w105` 复跑，
`plane_after_source=test_plane_w41c_final.xml`）。

## 5. 三条 AC 自测红是**真阴性**，不要"修"

同批里 `test_ac2_case_probe.py::test_self_test_closes_and_every_break_flips` 仍红，`--self-test` 指出
AC2-02 / AC2-04 / AC2-07 三条的 closure 读数是 `gap`（`breaks=N flipped=N`，反例臂全部咬住，但闭包
本身不成立）。这是"这三个验收项确实未闭合"的信号，不是夹具过期：它们的 closure facts 一旦成立就会
读数变化，而当前事实面并不成立。本轮**没有**动它们；把 `gap` 改成 `holds_offline` 或把自测改成
"允许 gap" 都是改判定，须 owner 签。




## 判定

```
objective_complete=False reasons=['residue_owner_gated=11', 'providers_without_rows=3', 'subagent_over_cap=4']
arms=10 arms_passed=10 PASS=True
```

三条原因各自都是 owner 动作或既成事实，脚本不替它们做决定：

1. **residue 11 行**：4 行两处通道都要求 `api_key`（越出「仅公开无凭证 GET」边界）、5 行已测无答复
   （sa 空信封不可反驳 / wsj 本机三次未建立 TCP）、2 行未测（stockgrid 无记录端点、AvailableIndices
   0 endpoints）。分档见 C145 `face.md` §7 与 `w67` 载体。
2. **3 个 provider 无行**：biztoc / stockgrid / wsj，无包且无 accepted 行；两台独立仪器（w72/w73）一致。
3. **子代理 34 > 30**：本 goal 实测越界 4 次，不能再派单，只能如实留档。

## 这一页能防止的两种误读

* **"339/350 就是没做完"**：339 是当前台账下的天花板（11 行全部有实测闸门），不是 11 行"忘了做"。
  记分板把 `residue` 与 `accepted` 放在同一行，比值与原因同屏。
* **"测试平面 28 条红"**：这 28 条与 wave 39 的 final 平面逐 id 相同（`new=0`、`fixed=0`），
  它们随波次移动的不是数量而是"谁在红"——所以这里给的是集合相等，不是计数相等。

## 重跑

```
PYTHONPATH=. /Users/yunjinqi/opt/anaconda3/envs/py313/bin/python \
  docs/evidence/C147/wave41-objective-scoreboard-20261020/probes/w105_objective_scoreboard.py
```

任何一项漂移都会让对应的 A–J arm 变红；`J_equality_arm_can_bite` 用内存里 +1 的 accepted 证明
"相等"这个谓词不是恒真。
