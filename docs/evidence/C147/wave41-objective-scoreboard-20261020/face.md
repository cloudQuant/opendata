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
| 测试平面 | **28 failed，与 wave 39 逐 id 相同（new=0）** | 两份 junit 经 `w51` reader 取集合，且与 `w103` 载体相等 |
| 子代理 | **34 次 / 上限 30** | `w74`（7/7 arms），超 4 次记为违反 |

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
