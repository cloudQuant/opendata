# C90 — cast/rescale 重锚之后：present 集从 10 涨到 12，而进入覆盖的行数是 0

本轮改了三份字节：`scripts/quality/model_capability_census.py`（两条 `CapabilityEntry` 重锚，22+/10−）、
`capability-roadmap.json`（重 derive，194+/206−，163327 → 163742 字节）、
`tests/test_model_capability_census.py`（`read_from_disk` 的 pin 从 5 个名字改成 7 个，9+/5−）。
新增两台仪器与三份逐字档案（本目录）。全部离线：未发一个请求，未设 `OPENDATA_ALLOW_LIVE_E2E`，
无 DDL/DML，未 `git push`；OpenBB 树只作输出契约对照；用户的 `README.md` 与四份
LICENSE/DISCLAIMER 草案仍未纳入任何提交。

## 1. 结论先说

| 面 | C89（`e05d1da` 的 roadmap 字节） | C90（工作树） |
| --- | --- | --- |
| registry 状态组 | `present=10 absent=39 drifted=0 unanchored=1` | `present=12 absent=37 drifted=0 unanchored=1`（四组和仍为 50） |
| `shipped_cover_rows` | 4 行 | **4 行，逐 id 相同**（新增 0、移除 0） |
| `rows_unlocked_by_greedy_prefix` | 183 | 183 |
| `rows_still_locked_after_engine_pool` | 91 行 | 91 行，逐 id 相同 |
| `rows_outside_engine` | 82 行 | 82 行，逐 id 相同 |
| 贪心阶梯步数 | 29 | **27**，`LADDER_DROPPED=['columns.cast','columns.rescale']`，`LADDER_ADDED=[]` |
| 引擎池 | 179 行 | **179 行，id 集合相等**（`POOL_ONLY_BEFORE/AFTER=[]`） |
| 阶梯里行的归属 | — | **18 行被重新指派到别的 rung** |
| `built_but_unused` | 3 | **5**（cast/rescale 进去了：present 而生产声明 0） |
| 提及数 / 标签面 | cast 32 / rescale 30；`raw_label_mention_count=1169`、`labels_supported=102`、`canonical=50` | 逐项相同（重锚不动标签） |

一句话：**把两个内核钉成 present 是诚实的（它们真的能跑），但它没有把任何一行搬进「今天可声明」。**
本轮把这个 0 拆成两个不同的 0 分别举证——一个是结构上的空集，一个是读数。

## 2. 仪器一：两条 `why` 说的限制还是引擎的限制吗

carrier：`cast-rescale-anchor-honesty.py`（438 行）→ `cast-rescale-anchor-honesty.txt`
（56 行 / 12341 字节，`sha256 5633e5bb…d97266`；四条腿
`RUFF_RC 0 / FMT_RC 0 / MYPY_RC 0 / BANDIT_RC 0`；`PYTHONHASHSEED ∈ {0,1,4242}` 三次运行逐字相同）。

它不接收任何结论，全部自己执行：

- **10 条执行臂**（旧 `why` 声称不存在的表面）：`apply_cast` 的 epoch 毫秒在 `UTC` →
  `date(2024,7,4)`、在 `America/New_York` → `date(2024,7,3)`（时区是声明的，不是本地时区猜的）；
  `date_string('%m/%d/%Y') '07/04/2024'` → `date(2024,7,4)`、`'%Y-%m' '2024-03'` → `date(2024,3,1)`；
  `apply_scale` `Decimal('4.33')` → `Decimal('0.0433')`、`strike_per_mille 43300` → `Decimal('43.3')`；
  `resolve_output_units('percent', scale)` → `'fraction'` 而 `(…, None)` → `'percent'`；
  **过生产接缝** `normalize_record` 的 cast stage → `month=date(2024,3,1)`、scale stage →
  `yield_rate=0.0433`。后两条是关键：锚点不是「函数存在」，是「声明式接缝真的会调用它」。
- **4 条拒绝臂**（证伪旧文字的载体）：`DeriveStage(kind="pivot")` → `ValueError: … is not one of
  ['cast','scale']`；`kind="cast"` 只带 `ScaleSpec` → `must carry a CastSpec, got None`；
  `ColumnSpec(..., format=…)` / `ColumnSpec(..., scale=…)` → `TypeError: ColumnSpec.__init__()
  got an unexpected keyword argument 'format'`（`scale` 同形）。最后两条就是旧 probe 永远数到 0 的原因：
  它们查的字段在 `ColumnSpec` 上不存在。
- **COUNT 面**（由本仓自己的扫描器读，`modules=162`，`exclude_parts=['_engine','_vendor','__pycache__']`）：
  活载体 `DerivePlan` → **1**（`opendata/data/providers/intrinio/specs.py`），这台仪器的正向对照，
  证明后面的 0 不是哑计数器；`DeriveStage kind="cast"` → 0、`kind="scale"` → 0；
  旧 registry probe `ColumnSpec keyword=format` → 0、`keyword=scale` → 0。
  于是 `REGISTRY_DECLARATIONS {'columns.cast': 0, 'columns.rescale': 0}` 与
  `VERDICT provider_declarations_of_stages=0`：**内核 present，但没有任何一列在生产上声明过它。**

## 3. headroom：先说清哪个 0 是空集，哪个 0 是读数

重锚之后 `verify_registry` 已把 cast/rescale 读成 present，所以「再把它们假设成 present」的
arm B 结构上必然返回空集：

```
ARM A shipped_cover=4  ARM B shipped_cover=4 newly_covered=0  ARM C shipped_cover=4 newly_covered=0
```

**这个 0 不能当证据**，本轮把它登记为退化而不是结论（我先前正是拿它当结论的）。能非零的对照是
arm M（把 registry 里所有 `engine_work` 能力都假设 present）：`ARM M shipped_cover=192
newly_covered=188`，`LADDER … M=0 cumulative M=192 still_locked M=82` —— 同一个计数器、同一份
census，读得出 188。

真正非退化的面是**提及者普查**（本轮补的）：

```
MENTIONERS n=57 pending rows list columns.cast or columns.rescale (cast 32 + rescale 30 mentions, 同时列两者的行 = 32+30−57 = 5)
MENTIONER-BLOCKERS distinct=19 [('flow.fan_out',30), ('rows.order_limit',23), ('columns.null_sentinel',15),
                                 ('flow.branch',13), ('flow.preflight_lookup',12), ('outside.anti_bot',11),
                                 ('flow.join',10), ('request.body_pagination',8), ('shape.columnar',7),
                                 ('outside.bespoke',5), …]
MENTIONERS-IN-COVER n=0 []
CONTROL forged=C90-FORGED-cast-only needs=['columns.cast'] in_cover_after_forging=1
```

也就是：列 cast/rescale 的 57 行，每一行都**至少还需要一个仍读作 absent 的能力**，最常见的阻塞是
`flow.fan_out`（30 行）和 `rows.order_limit`（23 行），而不是本轮动的这两个。`MENTIONERS-IN-COVER`
的 0 因此配了一个当场可翻面的正向对照：往 census 的**内存副本**里塞一行需求集只有 `columns.cast`
的伪造行，同一个谓词立刻读出 1（伪造不写盘，档案可复算）。

最后一张面直接回答「C89 说这次重锚会翻哪三行」：

```
C89-PROMISED OBB2-benzinga-AnalystSearch         in_cover=False still_missing=['columns.null_sentinel','rows.filter_comparison']
C89-PROMISED OBB2-federal_reserve-YieldCurve     in_cover=False still_missing=['columns.null_sentinel','rows.filter_comparison','rows.order_limit']
C89-PROMISED OBB2-government_us-TreasuryAuctions in_cover=False still_missing=['columns.null_sentinel','request.serialised_param']
```

三个 id 是从 C89 的记录引来的引用（§45 line 738 与 `oecd-blocker-recheck.md` §7 第 3 条），
`still_missing` 是本轮在重锚后的树上重测的，**与 §45 当时发表的阻挡集逐字相同**——
即 #81 补标签与本轮重锚两件事叠加后，那三行的阻塞原因没有变，只是不再是「因为标签少报而假 ready」。
仪器还带一条矛盾即死的守卫：若某行 `still_missing` 为空却不在覆盖里，判定面与谓词不一致，直接 abort。

顺带把覆盖标志与存储判定互相矛盾的两个方向都打了：
`IN-COVER-BUT-PENDING` 4 行（`cboe-EquitySearch`、`federal_reserve-SvenssonYieldCurve`、
`intrinio-CalendarIpo`、`intrinio-HistoricalDividends`，都是 `expressible_today=False` 而 need 集已被
present 覆盖）——这是任务 #84 的输入；`EXPRESSIBLE-BUT-NOT-COVERED count=0` 是另一个方向（存储说行
但覆盖不认），本轮读 0。

## 4. 仪器二：C89→C90 的移动面，两个 artifact 都从 git/disk 读

carrier：`roadmap-face-diff.py`（422 行）→ `roadmap-face-diff.txt`（157 行 / 14434 字节，
`sha256 02002a23…5e2a0aac`；49 PASS / 0 FAIL，四条腿全 0；`PYTHONHASHSEED ∈ {0,1,7,12345}`
四次运行逐字相同）。前态是 `git show e05d1da:…/capability-roadmap.json` 的字节，后态是工作树字节——
**没有一个数字来自我的记忆**。它比第 1 节的表更强的地方在于逐字段与逐 id：

- **逐字段 reading 面**（两条 entry × 13 个字段全打）。cast：
  `proof_file 'http_json.py' → 'value_transform.py'`，`anchor 'def _column_type' →
  'def apply_cast(cast: CastSpec, value: object) -> object:'`，`anchor_line_number 131 → 250`，
  `cross_file '' → HTTP_JSON_REL`，`cross_anchor '' → 'value = apply_derive(plan, value)'`，
  `read_from_disk False → True`，`claim False → False`，`production_declarations 0 → 0`，
  `status 'absent' → 'present'`。rescale 同形：`spec.py:210 'units: str = ""' →
  value_transform.py:399 'def apply_scale(scale: ScaleSpec, value: object) -> Decimal:'`。
  两行旧锚点的**原文字节**也打出来了，便于 reviewer 核对旧限制是不是真的挪走了。
- **恒等式与总量**：`POOL_SIZE 179→179`、两侧 id 集合相等、`SUM_NEWLY 179/179`、
  `ARITHMETIC pool=179 + shipped_cover=4 = cumulative=183（两轮都是 183）`、
  `pending 274 / expressible_today 5 / unlocked_prefix 183 / no_engine_work 12 / modules_scanned 162`、
  `METHOD_NOTE_UNCHANGED=True`、schema 与 `round_id` 从 `C89` 改成 `C90` 是唯一的标量变化。
  四个行集（`shipped_cover_rows`、`rows_outside_engine`、`rows_still_locked_after_engine_pool`、
  `rows_with_empty_need_set`）按 id 逐一比，`added=[] removed=[]`。
- **唯一的移动 = 18 行重指派**，其中 13 行正是 C89 记在 `columns.cast`（6 行）/
  `columns.rescale`（7 行）名下的那批，两步从阶梯上消失后它们被搬到
  `flow.branch 4→5`、`flow.fan_out 9→15`、`flow.join 5→12`、`rows.order_limit 7→6`；
  另外 5 行是 `rows.order_limit` 的归属随次序变化而搬动。`CREDITED_AFTER` 两行把这件事逐字打了出来。
  一行也没有离开引擎池。

五台对照保证这些 PASS 不是恒自行成立的：

1. **自比**（同一 artifact 比自己）：池相等、重指派 0 条。
2. **删一行**：身份面立刻变 False。
3. **改名一条归属**（`tamper_row=OBB2-alpha_vantage-EquityHistorical`）：重指派从真实 18 变 19，
   且差集恰好只有被改的那一行——`CONTROL_TARGET self_pool=179 real_reattributed=18 tampered=19`。
4. **陈 revision 拒绝**：把前后指向同一轮 ⇒ `StaleRevisionError: the pre-anchor revision already
   carries round C90; point PRE_ANCHOR_REV at the commit whose tree holds the roadmap written before
   the re-anchor`。
5. **伪造双记功**：注入一份把 `R-1` 记给两个 capability 的 ladder ⇒ `DoubleCreditError: R-1 is
   credited twice: a.first and a.second`；真实 artifact 走同一条检查、通过。

## 5. 我自己写错的话，原句留着

- §45 line 744：「**仍未量的是两件与标签无关的事：`read_from_disk` 是否真的会走 cast/rescale 字段**」。
  这句是范畴错。`read_from_disk` 是 `CapabilityEntry` 的字段，由 `verify_registry` 消费，
  不是引擎函数，不存在「走哪个字段」的问题；把它翻成 present 的机制是「anchor 在 proof_file 上命中
  + `claim=False`」，本轮实测打印在 `ENTRY … read_from_disk=True status=present present=True` 一行。
  同句里的第二问（`transpose_wide_table` + `pivot_series` 是否产出长记录）与标签无关、仍未量，归 #80。
- 本轮我先只发布 `CAST_WHY_REFUTED=True / RESCALE_WHY_REFUTED=True` 与 arm B 的空集来支撑
  「headroom=0」，那是拿结构空集当证据；第 3 节的 `MENTIONERS-IN-COVER` + 伪造对照才是替代证据。
- **我自己造的一份不可复算的档案**：`roadmap-face-diff.txt` 第一版入档后，我按纪律把新鲜运行与
  档案字节再比一次，读到 `ARCHIVE_STALE` —— 差异只有 3 行，全是
  `PASS PRESENT_ADDED={'columns.rescale','columns.cast'}` 这类**集合 repr**：`require()` 直接
  `{got!r}` 打印 set，而 set 的迭代顺序随进程 `PYTHONHASHSEED` 变。判定本身没坏（两侧都是集合、
  `==` 成立），坏的是「档案可逐字复算」这句话——两次运行碰巧同序并不代表确定性。
  修法是在打印路径上加 `show()`：set/frozenset 一律按排序后的元素渲染，比较口径一个字没动。
  修完用**强制不同的 hash 种子**当对照：face-diff 在 `{0,1,7,12345}` 四次运行 md5 同为 `305771de…`，
  honesty 在 `{0,1,4242}` 三次同为 `18b94aa7…`。原先那份 14458 字节的档案因此作废并被替换。
- **我自己引入的门禁红**：改完共享脚本没跑 pre-flight，`make a2-check` rc 2 ——
  `E501 Line too long (101 > 100) scripts/quality/model_capability_census.py:1466`（重 wrap `why`
  文本时留的）。修完 rc 0。两台新仪器因此也当场跑齐四条腿，bandit 不是无输出而是确实读了这份字节。

## 6. 命令 → 读数（任何人可重跑对表）

```bash
PYTHONPATH=. python docs/evidence/C90/cast-rescale-anchor-honesty.py     # rc 0，56 行面
PYTHONPATH=. python docs/evidence/C90/roadmap-face-diff.py               # rc 0，49 PASS / 0 FAIL
PYTHONPATH=. python scripts/quality/model_capability_census.py --check   # --check verdict: PASS
PYTHONPATH=. python -m pytest tests/test_model_capability_census.py -q --no-cov   # 21 passed
make a2-check / ledger-check / zero-dep-check / quality-ratchet / test-cov
```

| 腿 | rc | 读数 |
| --- | --- | --- |
| `a2-check` | 0 | `ok ruff check / ok ruff format --check / ok mypy / ok bandit`（本轮先红后绿，见 §5） |
| bandit（两台仪器） | 0 | `Code scanned: Total lines of code: 740`、`Total lines skipped (#nosec): 0`、`No issues identified` |
| `ledger-check` | 0 | `130 items / proven 112 / gap 18`，状态字段名 `items[*].state`（档案里没有 `status` 键） |
| `zero-dep-check` | 0 | `666 file(s) walked (opendata=339, _vendor=325, opendata_client=2)` |
| `quality-ratchet` | 0 | `OK: quality debt did not increase`（ruff_selfdev 129/148、mypy_selfdev 0/0、bandit_selfdev 0/1、ruff_ported 1867/2188、direct_http_ported 1071/1071；带 `NOTE: run --update to freeze the new low`） |
| `test-cov`（member 12） | 0 | `22459 passed, 148 skipped in 452.40s (0:07:32)`，`Total coverage: 89.88%` |
| census `--check` | 0 | `check={'drifted_capabilities': [], 'failures': [], 'unanchored_capabilities': ['outside.source_absent']}`，`row_count_measured=279 / expected=279 / guard_fired=false / unique_task_ids=279` |

与 §45 发表的门禁第 8/9 遍相比，测试数与覆盖率**逐字相同**（`22459 passed, 148 skipped`、`89.88%`，
只有墙钟从 450.46s 到 452.40s）。这不是巧合而是本轮写入的定义面：本轮只改 registry 的两条 entry
文字/锚点/probe 与 roadmap 的 derive 结果，没动 `opendata/` 或 `tests/` 的任何行为字节，
唯一被改的测试文件是那条 `read_from_disk` 名单 pin（5 → 7 个名字）。

`make evidence-traceability` 本轮读 rc 2：`NEW VIOLATION: docs/evidence/C90/#narrative` 加 5 条
untracked。这条腿的判据是「每个 `docs/evidence/<round>/` 至少一份 `.md` 叙述，且不留 untracked」，
所以它只能在本文与 C90 全部字节入提交**之后**闭合——本轮不假装它绿。

## 7. 本轮结束时仍然打开的面

1. **#84**：4 行 `IN-COVER-BUT-PENDING` 的 need 集未经上游 cite 逐行复核，`shipped_cover_rows=4`
   这张面因此描述的可能是少报的 need 集，与 OECD/TMX 那次同形。
2. **#80**：`OBB2-federal_reserve-SvenssonYieldCurve` 的需求集修复；`transpose_wide_table` +
   `pivot_series` 是否产出长记录仍未量（这才是 §45 那句话里剩下的真问题）。
3. **federal_reserve 的 census-vs-ledger 冲突照旧在册**：`specs.py` 仍把 YieldCurve 记 `NOT_DECLARABLE`
   且仍写「Melting is not declarable」，而 `path.explode` 实测 present——现行理由是策展不是引擎缺口，
   改写要先量，本轮没有动这两行文字。
4. **`rescale` 的诚实性边界**（§45 已记，本轮未推翻）：`value_transform.py` 拒收 float 输入，
   而上游把百分比字段先过 `float()`，所以把 rescale 钉成 present 只对小数串/整数发布值诚实。
5. `UNPROVEN: ['outside.source_absent']` —— 设计如此（文件系统事实，故意不锚定）。
6. 50 个 canonical 能力未收敛；146 行 / 22 包的实现波、350 行 live 轴（在授权闸门外）、
   member 1 `brand-check`、member 11 `acceptance-probe-check`（约 1300s，本轮未跑）仍未开始/未跑。
