# wave-31 step 0 — plane 2 (DEV_DONE) 拆轴，以及下一批的裁决

日期 2026-10-18 · HEAD `22f31a9e`（dev，ahead 12，**无 push 授权**）· 解释器
`/Users/yunjinqi/opt/anaconda3/envs/py313/bin/python` · 判定件 `scripts/quality/ac2_case_probe.py`（本仪器只 import，不改）
· plane 1 身份集 `5168e1e723999312`（未动）

## 0. 这一步要回答什么

wave-30 发布的判据是 `witnessable_today=0`，出处是终代动作图载档
`docs/evidence/C130/plane1-action-map-rebase-20261018/runs/wave30-step5a-residue_axis_census_finalgen.txt`
（本仪器 E 臂逐字解析它，并门禁它仍是终代）。
task #76 的原话要求是：跑仓库自带的判定件、逐字读面，然后裁决下一波最便宜的动作是 plane-2（契约套件）还是
`implement_missing_model` 那 19 行的 provider 代码。此前只有 AC2-10 的一个 `state=gap` 汇总数，没有任何人拆过
"这个 gap 到底由哪几个轴决定"。本记录给出第一个拆开的 plane-2 面。

## 1. 仪器与重跑

探针 `probes/plane2_dev_done_axis_census.py`（493 行 / 21509 B / `e274897d3e7d72eb`；ruff check + format --check + py_compile 三零）。
逐字 argv 就写在载档第 2 行，重跑：

```
cd /Users/yunjinqi/Documents/new_projects/opendata
PYTHONPATH= /Users/yunjinqi/opt/anaconda3/envs/py313/bin/python \
  docs/evidence/C131/wave31-plane2-face-20261018/probes/plane2_dev_done_axis_census.py
```

终态载档 `runs/wave31-step0-plane2-axis-census.txt`（141 行 / 14079 B / `022bd5186aa69c81`），末两行
`plane2_axis_census_face arms=15 arms_passed=15 arms_failed=0 … PASS=True` 与 `plane2_axis_census_exit=0`。

## 2. 面（载档行号即可复核）

仓库自带读数——载档 6–19 行逐字抄（载档第 5 行是 25 个契约测试文件名，太长此处指向载档，不做改写）：

```
0  contract_test_functions=606
0  dev_done=17
0  dev_done_label=5
0  e2e_marker_declared=True
0  engine_declared_rows=17
0  gate_live_confirmation=True
0  in_progress=13
0  label_without_witness=0
0  label_without_witness_ids=-
0  ledger_rows=350
0  live_verification_run=0
0  not_run=332
0  obb2_task_ids=350
0  state=gap reason=failed: all 350 are DEV_DONE with an executed witness; no row left at NOT_RUN
```

| 臂 | 读数 | 意义 |
|---|---|---|
| A（载档 20–37） | `count=17 of=350 providers=6 histogram={'cboe': 3, 'econdb': 1, 'federal_reserve': 6, 'intrinio': 5, 'tiingo': 1, 'tradingeconomics': 1}` | 运行期声明总体 |
| B（38–40） | `suite_keys=202 model_shaped=25 case_label_keys=177 positive=21 refusal=21 both=21`；`declared_without_face=0 []`；`identity model_shaped=25 = declared 17 + probe_rows 8` | 声明 ⇒ 正反例齐全，无一枚缺面 |
| C（41–73） | `2x2={'accepted_declared': 17, 'accepted_not_declared': 268, 'residue_declared': 0, 'residue_not_declared': 65} declared_outside_identity_set=0` | 17 枚声明是 plane-1 已验收 285 的**真子集**；残留 65 行一枚都不声明 |
| D（74–87） | `status_histogram={'DEV_DONE': 5, 'IN_PROGRESS': 13, 'NOT_RUN': 332} witnessed=17 declared_and_labelled=5 declared_not_labelled=12` | 台账只盖了 5 枚，另外 12 枚"已声明且已有正反例"仍写 NOT_RUN |
| D2（88–101） | `columns_compared=27 value_set_differing=13 uniform_split=0 []` | 27 列里没有任何一列能自证这枚标签 |
| E（102–122） | `parsed_rows=65 implement_missing_model=19 expected=19`；`by_provider={'government_us': 1, 'tmx': 16, 'yfinance': 2} rows_in_a_provider_that_already_declares_something=0 of=19` | 19 行分布在三个**今天零声明**的 provider |
| F（123–127） | `F1 17 -> 16`、`F2 17 -> 16`、`F3 label_without_witness 0 -> 1`、`F5 uniform 0 -> 1 ['credential_fields']`、`F4_restore caches_identical=True after=17` | 四条内存再上弦，每条都能移动 |
| G（128–129） | `contract_test_files=25 functions_in_them=606 suite_executed_for_faces=1 live_verification_run=0` | 本仪器的地板：面只来自 `tests/test_provider_model_contracts.py` 一套 |

三条结论：

1. **plane 2 的轴是引擎声明，不是"开发完成"。** `dev_done=17 == engine_declared_rows=17`，且
   `declared_without_face=0`（B）。也就是说 AC2-10 的见证腿今天没有提供任何超出"这行是声明式 ModelSpec"的信息；
   把 606 个契约测试函数（G，25 个文件）都算进来也不会改变这一点，因为 `witnessed_dev_done` 只认
   `engine_declared_keys()`（`scripts/quality/ac2_case_probe.py:1319` ← `catalog.engine_declared_models()`
   `opendata/data/providers/catalog.py:212` 只收 `descriptor.fetcher_bindings` 里能加载出 `ModelSpec` 的对象）。
   把见证口径放宽到其余 24 套 provider 契约套件，就是**放松判定件**，本轮拒绝。
   连带读数：C 的 `accepted_not_declared=268`——plane 1 已验收的 285 枚里，268 枚在引擎里取不到声明式 ModelSpec
   绑定；手写形状的一例就是 tmx 的 4 枚已落地行，它们带 `row_model: ClassVar`
   （`providers/tmx/models/fetchers.py:293/347/397/554`），而 `engine_declared_keys` 对 tmx 记 0
   （载档 69 行 `provider tmx rows=24 accepted=4 declared=0`）。
   所以只要继续这个形状，AC2-10 的 `reason`（"all 350 are DEV_DONE with an executed witness"）不可达；
   把 268 枚手写行改造成声明式 ModelSpec 是架构问题，本记录只把它作为**待裁决项**列出，不在此处代答。
2. **book 与 runtime 漂移 12 行，且台账无法自证。** D2 用 27 列逐列比较盖章 5 枚与未盖章 12 枚的取值集合：
   13 列不同，但 `uniform_split=0`——没有任何一列是"5 枚一个值、12 枚另一个值"。F5 证明这个 0 是可移动的：
   在 deepcopy 里把 12 枚未盖章行的 `credential_fields` 统一改成 `api_key`，`uniform` 立刻 `0 -> 1`
   并命中预期列名，门禁 `no_ledger_column_explains_the_dev_done_label` 转红。所以 5 与 12 的差别不是"多一道腿"，
   纯粹是手维护台账的历史状态。附带读数：载档 0 行的 `live_verification_run=0` 说明这 5 枚盖章行也没有声称跑过
   线上验证——这与既有的"34 枚消费用途 UNCONFIRMED"裁决不冲突。
3. **17 枚全部落在 plane 1 的 285 之内**（C：`residue_declared=0`）。所以 plane 2 现在没有任何一行是 plane 1 尚未验收的；
   两面的差集为空，#19（两面口径不同）在**当前总体**上不再是阻塞项，它只是限制了"手写 fetcher 的行永远进不了 plane 2"。

## 3. 逐 take 溯源（含我错的那次）

| take | 载档 | 行/字/摘要 | 状态 |
|---|---|---|---|
| 1 | 无 | — | **崩溃且无载档**：`labelled = {r for r in rows …}` 对 dict 取集合 → `TypeError: unhashable type: 'dict'`，traceback 停在 `_arm_d`；stdout 重跑复用了同一文件名，崩溃现场只剩我会话里读过的那份转写，标记为**不可复算** |
| 2 | `runs/wave31-step0-take2-b-arm-polluted.txt` | 123 / 18291 / `e0d2f630d6a69607`（stdout 125 / 18343 / `73406553b31cfbd0`） | B 臂在 202 个 junit id 上计数，打印 `face_not_declared=185`，读起来像"185 枚有面却未声明的模型"；当时 `arms=12 … PASS=True`——**绿色 PASS 掩盖了口径错**，门禁没抓到，是我复审自己的名词表才发现的 |
| 3 | `runs/wave31-step0-take3-b-arm-corrected.txt` | 124 / 10554 / `73ca9b718bf74468`（stdout 126 / 10606 / `fde6697948db4b53`） | 先按 `^[a-z][a-z0-9_]*::[A-Za-z][A-Za-z0-9_]*$` 拆分总体：25 枚模型形状 / 177 条参数化标签，未声明列表从 185 收敛到 8；新增门禁 `junit_key_split_is_exact` |
| 4 | `runs/wave31-step0-take4-stdout.txt` | 127 / 10781 / `17bb89d164290882` | 全绿（arms=13）。探针 B 臂 docstring 里的解释数字（"185 of 202"、"from 4 to 21"）是我没量过就写下的散文，改为实测 177/202、并说明 `positive/refusal=21/21` 本来就只落在模型形状标签上——拆分修的是关系列表，不是计数器 |
| 5 | `runs/wave31-step0-take5-docstring-only-stdout.txt` | 127 / 10781 / `17bb89d164290882` | 只改 docstring 后重跑：与 take 4 **字节相同**（同一摘要），证明该改动对输出中性 |
| 6 | `runs/wave31-step0-take6-d2-f5-stdout.txt` | 143 / 14131 / `2cddaaa1fb4cabe1`（载档 141 / 14079 / `022bd5186aa69c81`） | 加 D2 + F5 + 一条门禁：与 take 5 逐行 diff 为**纯增量**（只多 D2/F5/新门禁行，face 行 13→15 arms），原有 13 个读数一字未变 |

## 4. 下一批裁决

19 行 `implement_missing_model` 里，`government_us::TreasuryPrices` 与 16 枚 tmx 行是 `SEAM_REACHABLE`、
`module=True`（载档 E 的逐行读数），`yfinance::AvailableIndices` / `yfinance::FuturesCurve` 是
`SEAM_NEVER_USED module=False`。同时 `rows_in_a_provider_that_already_declares_something=0 of=19`。
这两个读数不矛盾，且必须分清：C130 的逐行字段是 `witness_module=True witness_entry=False name_in_package=False`
（例：`wave30-step5a-residue_axis_census_finalgen.txt:7` 与 `:15`）——模块文件存在（wave-30 给 tmx 建过一套），
但这一行在模块里没有见证项、模型名也不在包里，所以同一载档第 77/80 行才记 `F_witnessable_today count=0` /
`witnessable_today=0`。也就是说 17 行的"可达"只是**harness 能服务它**，取得见证的前提是先写代码再补见证项。

据此定本波动作：**写真实缺失模型（provider 代码），不是去补 plane-2 的见证面**。理由是可复算的，不是偏好——
plane 2 的见证腿等价于引擎声明（第 2 节结论 1），而这 19 行的 provider 今天零声明，所以它们既不动 plane 2 的轴，
也只能靠新增声明+代码来动 plane 1（残留 65 → 减）。tmx 现状盘点（同一批 grep/wc 实测，不是回忆）：包内
5 个 `.py`、共 866 行（`__init__.py` 5、`provider.py` 35、`models/__init__.py` 23、`models/client.py` 189、
`models/fetchers.py` 614）；`fetchers.py` 有 20 个顶层类，其中 8 个 `*Fetcher` 与 8 个 `*Query`；契约侧
`opendata/data/models/tmx.py` 是 8 个 `Tmx*` 契约类加 1 个 `_TmxContract` 基类。把这 16 枚缺失模型名逐个
在 `providers/tmx/` 与 `models/tmx.py` 里 grep：**16/16 命中 0 个文件**，即这批行既没有契约类也没有 fetcher，
是真的缺实现而不是缺声明。台账侧另有两点值得记：24 枚 tmx 行的 `upstream_shape_file` 全部是
`providers/tmx/openbb_tmx/__init__.py`、`upstream_shape_sha256` 全部是 `876a698409d8…`、`upstream_shape_line`
是连续的 45–68，也就是 OpenBB 的模型声明清单指针而非输出字段形状；且 24 枚 tmx 行在台账里**全是 NOT_RUN**，
连 wave-30 已落地并被 plane 1 验收的那 4 枚也没改过——与结论 2 的 book/runtime 漂移同源。对照件 OpenBB 在
`/Users/yunjinqi/Documents/new_projects/openbb/openbb_platform/providers/tmx`（clean-room：只作输出契约对照）。

下一步谓据与失效条件（wave-31 step 1）：先在**最便宜的一枚** tmx 行上量成本——判据是"producer → invariant 探针 →
recorder → merge → judge → 重数"这条链跑通且 plane 1 计数 `285 -> 286`；若 judge 后计数不动，则说明该行仍未取得
五相，按 `UNBOUND` 留在残留里并打印原因，**不翻标签**。任何需要 `OPENDATA_ALLOW_LIVE_E2E` 的腿一律保持 BLOCKED 并打印
拒绝，不作 skip；不放松任何判定件；不写台账标签；无 push。
