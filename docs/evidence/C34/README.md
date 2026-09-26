# C34 — `index_constituent` 退路的加宽样本对照，与 `as_of` 口径拍定（AC-3 / AC-10）

## 0. 本轮要收的账与结论

C24（`docs/evidence/C24/README.md` §3）给 `index_constituent` 记的是：成员集合与权重
0/4 不符（= 全部相符），但**两侧时间戳口径不同**（ths `as_of`=生效观测日、akshare
`as_of`=月末文件日），所以"本轮同样不翻"，并把口径列为转正的三个前置条件之一：

> 同形状 + 加宽样本的跨 vendor 等价读数 +（`index_constituent` 还要先定 `as_of` 口径）。
> 三条同时满足才动 `verified`。

本轮用户拍定口径为**「钉成清单日并翻正」**：`as_of` = 该源所发布清单的所属快照日。
口径已落进契约与两条腿（§2），加宽样本已跑完（§3），但**`verified` 没有翻**，因为
加宽之后的读数不支持它：C24 那句"成员集合逐只相同"是在**一个指数**（000300）上量的，
样本加宽到 21 个代码后有 4 个代码两侧成分不同。这是本轮最重要的事实——**决策是口径
决策，不是结论决策**，拍定"清单日"不等于允许把 `verified` 写成 `true`。

| 项目 | 本轮结果 |
|------|---------|
| `as_of` 口径 | 已拍定并落进契约 docstring + 两条腿 `notes`（可见于 `/sources`） |
| 加宽样本 | 21 个指数代码，两侧都是真机取数（csi + fuyao） |
| 可判读 | 13 个：9 个逐只相同，4 个不符（3 等量换入换出 + 1 不等量） |
| 不可判读 | 8 个：ths 指数目录解析不出该代码，CSI 权重文件有该指数 |
| `verified` 翻正 | **未做**（判据不满足，登记归因，未放宽判据） |
| 路由面 | 未动：国内 8 行仍是 0 条可降级退路（`tests/test_fallback_degradation.py` 13 例仍绿） |

## 1. 先量：C24 那份对照面今天还能不能跑（`fallback-recheck-2026-09-26.txt`）

跑之前先把"量不出来"和"量出不符"分开，因此重跑不是复述：8 个域的 `auto` 退路对照
读数是 **PASS=2（`index_constituent`、`stock_action`）／MISMATCH=3／NO_READING=3**，
`XCHECK_EXIT=0`，与 `CASES` 归档逐条相同。三个 NO_READING（`stock_daily`／`index_daily`／
`fund_etf_daily`）仍是 eastmoney K 线通道对本机拒答（502），**已连续第三轮**——它不是
"退路不合格"，是"这一半的判据今天量不到"，两者在归档里从未混写。

## 2. 口径落地：三处改动，全部是"公开优于主张"

| 位置 | 改动 | 为什么在这一层 |
|------|------|-------------|
| `opendata/data/models/index.py` | `IndexConstituent` docstring 钉定：`as_of` 是**清单所属日**；只有发布该日期的源才填它，不发布清单日期的源**在 `notes` 里声明缺口**而不是借一个日期 | 口径属于契约，不属于某个 provider；`as_of` 仍是必填字段，所以"声明缺口"是唯一合法出路 |
| `.../ths/models/index_constituent.py` | `notes="as_of is the observation day; this endpoint publishes no list date"`；docstring 删掉"as_of 语义已对照"这一句（C9 当时量的是行身份与集合，不是与退路同口径） | ths 端点唯一的时间戳是请求时刻（`opendata_fuyao/endpoints.py:746` 已写明是观测日）；把缺口写进 `notes` 才会在 `/sources` 上看得见 |
| `.../akshare/models/index_constituent.py` | `notes="CSI close-weight file; as_of is the list's own data date"`；类 docstring 记下 21 个代码的读数与"翻正需要重跑加宽且不留不符" | 退路侧填的确实是文件自己的 `日期` 列，两腿口径**不同**这一事实被登记，而不是被同一句话抹平 |

`authority.json` 未改：`index_constituent` 仍排 `["ths","akshare"]`——未翻正不等于撤回退路。

## 3. 加宽样本：21 个代码，三态判定（`index-constituent-generality-sweep.txt`）

判定面 `index_constituent_generality_sweep.py`（本轮新写，只读、不写数仓、不打印密钥）。
三态沿用 `akshare_fallback_cross_check.py` 的不对称口径：`PASS`=成员集合逐只相同；
`MISMATCH`=集合不同或退路自身不自洽（权重不是每行都有值、或不合计到 100）；
`NO_READING`=一侧未答或答空——**不参与任何方向的结论**（C28）。

| 判定 | 数量 | 代码 | 读法 |
|------|------|------|------|
| PASS | 9 | 000300 沪深300(300)、000016 上证50(50)、000905 中证500(500)、000852 中证1000(1000)、000010 上证180(180)、000913 300医药(19)、000928 800能源(24)、000934 800金地(94)、000032 上证能源(30) | 全部是**按半年调样**的家族；共同成员=两侧成员数，两个方向的不符都是 0 |
| MISMATCH（等量） | 3 | 000688 科创50（5/5）、000689 科创材料（4/4）、000698 科创100（10/10） | 两侧各 50/50/100 行、权重都合计 100.00，但成员**等量换入换出** = 两份不同日期的清单在按季调样指数上的典型形状 |
| MISMATCH（不等量） | 1 | 000680 科创综指：ths=579 / akshare=578，仅 ths 认 688646 | **不是换仓形状**，本轮未归因，登记为待办（§6） |
| NO_READING | 8 | 000988、930050、000963、000825、000922、000919、000949、000801 | `THS_INDEX_SYMBOL_UNRESOLVED`：ths 指数目录不认这些代码，CSI 权重文件发布它们 ⇒ 这量的是**两份目录的覆盖差**，不是数据错 |

`as_of` 一列**报告而不判定**（本轮已拍定口径，再判它等于用旧口径打分）：主源 13 个可判读
代码全为 2026-09-26（观测日），退路全为 2026-08-31（8 月文件日）。30 天不到的差在半年调样
家族上不改变成员，在按季调样家族上正好跨过 9 月生效日——这就是 §3 表格的形状来源。

**脚本自己的判定也红过一次**：首版把 000688 钉成 `PASS`（照抄 C24 的单代码结论），
第一跑当场报 `[MISMATCH] 000688 …（归档判定=PASS）`、`DRIFT：1`、退出码 1。修的是
**我的 pin**（改成实测值），不是判据。这条留在反证里（§5）。

## 4. 守卫：口径与判据都进 `tests/`（`tests/test_index_constituent_asof.py`，21 例）

C29 的规矩——规则不许只活在证据目录里。离线可跑、不联网，五组：

1. **口径声明面**：两条腿的 `notes` 各自写死自己填的是哪一天，且**两句必须不同**
   （同一句话就是把差异盖回去）；契约的 `as_of` 仍必填（这是"声明缺口"而非"留空"的理由）。
2. **行为面**：akshare 腿的 `as_of` 逐行取自文件 `日期` 列（夹具：两行 6-30 + 一行 3-31 +
   一行无日期），且**永远不等于调用当天**；无 `日期` 的行被丢弃而不是补一个日期。
3. **转正判据面**：从归档读回三态集合（9/4/8 逐码钉死，钉集合不钉数量，因为调样会移动
   成员数），并断言 `verified is (归档里 MISMATCH 集合为空)` —— **双向**：今天脏所以必须
   `false`，一份干净的加宽读数则必须把它翻成 `true`。主源腿的 `verified=true` 单独断言，
   不被退路的缺口连带改动。
4. **形状面**：3 个等量换入换出（`仅 ths == 仅 akshare > 0`）、1 个不等量（1/0）分开钉；
   PASS 组必须是 `(0, 0)` 而不是"差不多"；8 个 NO_READING 必须是 `("NO_READING", None, None)`，
   即它们既不计入相符也不计入不符。
5. **泛化 claims 面**：`scripts/ops/akshare_fallback_cross_check.py` 的 `index_constituent`
   note 必须写明"该次调用（000300）"与"单代码 PASS 不等于跨指数成立"并指向本目录 ——
   防止下一次有人把它读成跨指数结论。

## 5. 反证：六处变异，两遍才跑对（`guard-mutation.txt`）

第一遍（M1–M6）**先量出守卫与变异自身的三处缺陷**，全部如实留在档里：

* 缺陷 1（守卫）：flip 那条写成"先断言归档非空，再断言 `verified`"，于是"归档变干净"这个
  世界只会在非空断言上红，**不会反过来要求翻正** ⇒ 改成双向 iff；
* 缺陷 2（变异）：M6 第一遍只替换了 `captured_at=` 的值，仍满足 `startswith("captured_at=")`
  ⇒ 21 例全绿是**变异没生效**，不是守卫恒真；
* 缺陷 3（变异）：M2 第一遍只把 000688 一个代码改成 PASS，剩 3 个不符 ⇒ 红的是钉表那条，
  不是 flip 那条。

第二遍（M1b／M2b／M6b／M7）逐一生效，且每个文件跑完按 sha256 还原一致：

| 变异 | 结果 |
|------|------|
| M1b 归档脏却写 `verified=True` | 1 红：`test_the_akshare_leg_is_verified_only_if_the_sweep_came_out_clean` |
| M2b 归档四处 MISMATCH 全改 PASS（加宽样本变干净的世界） | 3 红：flip 判据（反向要求翻正）＋ 三态钉表 ＋ `MISMATCH` 非空参数例 |
| M3 退路 `notes` 抄成主源那句 | 2 红：`notes` 声明例 ＋ "两句必须不同" |
| M4 退路 `as_of` 改成调用当天 | 3 红：逐行日期 ／ 不等于今天 ／ 无日期行丢弃 |
| M5 把泛化 note 还原 | 1 红：note 必须写明测的是哪一次调用 |
| M6b 整行删除 `captured_at` provenance | 1 红：归档可追溯性守卫 |
| M7 归档出现两处 `SWEEP_EXIT=0`（读数被拼接） | 1 红：同一条守卫 |

## 6. 登记（未放宽的判据、没量到的面、顺带量到的新洞）

1. **000680 科创综指 579 vs 578**：唯一不是换仓形状的一条，本轮**未归因**。要归因需要
   第三个事实源（上交所/中证对该指数的官方成分表），不在这两条腿之间自证。
2. **`verified` 的翻正路径已唯一化**：等 9 月调样进两侧文件之后重跑
   `python docs/evidence/C34/index_constituent_generality_sweep.py`，只要还有 MISMATCH，
   脚本退出码 1、守卫用例同时红。预期窗口：CSI 9 月文件（约 2026-09-30 起发布）。
3. **eastmoney K 线三连拒**（`stock_daily`／`index_daily`／`fund_etf_daily`）：这三个域的
   退路面量，本轮仍只能"报告不能判定"。
4. **本轮顺带量到一个门禁静默洞（转 C35，任务 #43）**：根目录 `akshare/` 早在 A2 里程碑
   改名 `opendata_http/`，而 `pyproject.toml` 的 `[tool.ruff] exclude` 与
   `scripts/quality/a2_check.py:49 EXCLUDED_PARTS` 仍按**任意路径段**匹配 `"akshare"`，
   于是 15 个第一方模块 `opendata/data/providers/akshare/**`（本轮改的就是其中之一）
   既不进 `make a2-check`，也不进 `make lint`／`ruff format --check`；实测该树现存
   **6 条 `I001`（unsorted-imports）从未被门禁报出**。`opendata`/`scripts`/`tests` 三棵树
   里被这个段名规则误伤的**只有**这 15 个文件（`alembic`／`frontend` 无额外误伤）。
5. **我写的跑前宣告头有一处笔误**：`index-constituent-generality-sweep.txt` 第 10-11 行的
   判定面自述写"9 个代码因 ths 目录不认"，实测是 **8 个**（头部同一段写的"21 个"与脚本
   `CASES` 一致）。归档按"不改写已落档读数"的规则原样保留，正确读数以 §3 表格为准。

## 7. 复算

```bash
export PATH="$HOME/opt/anaconda3/envs/py313/bin:$PATH"; export PYTHONPATH=.
set -a; source .env; set +a                       # fuyao Key 只在 .env 里
python scripts/ops/akshare_fallback_cross_check.py            # §1 的 8 域对照（需网络）
python docs/evidence/C34/index_constituent_generality_sweep.py  # §3 的 21 码加宽（需网络）
python -m pytest tests/test_index_constituent_asof.py --no-cov -q   # §4 的 21 例（离线）
make gate                                                     # §8
```

## 8. 门禁

`gate.txt`（6,514 行，完整未裁剪，跑前 provenance 头在内）：**`GATE_EXIT=0`**，
`A2 files: 297` → `OK: A2 files meet the full A2 standard (ruff + format + mypy + bandit)`，
后端 **2,870 passed / 6 skipped**、coverage **86.76%**（阈值 84），前端
`Test Files 14 passed (14)`，`frontend-e2e` 段落跑完。

**6 条 skip 逐条归因**（都是 em 通道的搬运保真例，与 C27/C33 同一组、本轮一条没多）：
`stock_daily_raw`、`stock_daily_qfq`、`index_daily_em`、`fund_etf_daily_em`、
`test_qfq_factor_steps_are_the_recorded_ex_dates[em]`、
`test_qfq_synthesis_reproduces_the_official_series[em]` ⇒ 仍挂在任务 #14（em 网络对本机拒答）。

本轮相对上一轮（C33：2,849 passed / 86.75%）用例 +21、覆盖率 +0.01pp：新增的 21 例全部
来自 `tests/test_index_constituent_asof.py`，判定面脚本（`docs/evidence/**`）不计入分母。

## 9. 档案清单

| 文件 | 是什么 | 跑法 |
|------|--------|------|
| `README.md` | 本文件 | — |
| `fallback-recheck-2026-09-26.txt` | C24 那份 8 域对照面今天的完整读数（含三态汇总与 `XCHECK_EXIT=0`） | `python scripts/ops/akshare_fallback_cross_check.py` |
| `index_constituent_generality_sweep.py` | 本轮新写的加宽判定面（21 码 × 成员集合 × 退路自身权重自洽） | 见 §7 |
| `index-constituent-generality-sweep.txt` | 上面那份的归档读数（含 `as_of` 报告列、转正判据、`SWEEP_EXIT=0`） | 同上 |
| `guard-mutation.txt` | 守卫可证伪性两遍（第一遍的三处自身缺陷原样保留 + 第二遍六处生效 + sha256 还原） | 手动逐条植入后 `pytest tests/test_index_constituent_asof.py --no-cov -q` |
| `gate.txt` | `make gate` 全量输出，6,514 行未裁剪，`GATE_EXIT=0` | `make gate` |


