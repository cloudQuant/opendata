# C106 · 声明↔契约对齐：17 个引擎域 313 列，把「声明说了什么」逐列对上「被审行类说什么」

判定对象（产品面，不含本目录）：`opendata/data/providers/_engine/testing.py`、
`opendata/data/providers/federal_reserve/specs.py`、`opendata/data/providers/cboe/specs.py`、
`opendata/data/providers/intrinio/specs.py`、`tests/test_federal_reserve_obfr_spec.py`，基线提交
`4fda2d86516aa29dfe4aff3da63fbd7838d86606`。

## 1. 这一轮立的是哪条判据

`build_row_model` 只钉列的**名字集合**（`declared != contract_fields` 才 `ValueError`），不钉 kind、
不钉 required/nullable。于是「声明」可以和被审行类的字段各说各话，而任何测试都不会在声明时变红——
它只在 `fetch` 时才以 `*_SHAPE_INVALID` 爆出来。本轮把这条洞量出来，再逐列对上：

- **kind 轴**：列声明的 kind 所对应的 Python 类型，必须是行类字段注解所接受的类型之一；
- **required 轴**：`column.required == field.is_required()`；
- **nullability 轴**：`column.nullable ==`（注解是否含 `None`）。

唯一被文档化的例外是引擎本来就在读的时态面：`date` kind 的列，其行类字段是 `datetime` 时，
发布值就是午夜瞬时（`_engine/testing.py::_row_temporal_type`）。例外只在该列的字段确实打印
`datetime` 时生效，并且计数器 `temporal_exception_covered` 单独打印，防止「例外把一切都吞掉」。

## 2. 读数（`probes/align_probe.py`，`align_probe_exit=0`，全文见 `runs/align-probe-current.txt`）

```
identity: models=17 columns_checked=313 mismatched_columns=0 temporal_exception_covered=3
control-exception-off: mismatched_columns=3 (live 0 + covered 3) rows=3
control-kind-tamper: mismatched_columns=1 row=["federal_reserve::SOFR.date kind=int field={<class 'datetime.date'>}"]
control-nullability-tamper: mismatched_columns=1 row=['federal_reserve::SOFR.rate declared req=True nul=True / field req=True none=False']
arms: total=13 read_as_expected=13
causality: live_cell=1 disabled_branch_cell=2 body_under_disabled_branch -> refused:ProviderEngineError
identity: literal_members_under_test=(0, 1)
verdict: PASS
```

那个 0 不是空转得到的：三条对照臂都能把同一台仪器读出非零——关掉时态例外读 3（正好等于被例外吸收的
3 列）、内存里把一列 kind 改成 `int` 读 1、把一列 `nullable` 改成 `True` 读 1。三个 tamper 都只
`dataclasses.replace` 内存副本，磁盘字节未动。

判定探针自己也按 A2 面转绿（`ruff check` / `ruff format --check` / `mypy` / `bandit` 四条全 ok，
全文见 `runs/align-probe-static-planes.txt`）。那次改写只动 types、import 与布局，与归档面逐字比对
只有一行动（`arm: date_span=1 -> ok:1` → `ok:date_span=1`，是臂的标签而非读数），其余 22 行逐字相同。
同一次 a2 运行里仍有一条**与本目标无关**的红：「pinned Gitleaks file scan」在 `docs/evidence/C100/**`
的不可变证据快照上读到 leaks，对 `opendata/data/providers/catalog.py` 单目标复跑读到的是同一条 FAIL，
说明它是快照面而不是本轮字节面；记为既存红，本轮不改判定也不重钉快照。

## 3. 修复前同一台仪器读到多少（`runs/align-probe-head-census.txt`）

把本轮改过的五个文件退回 `4fda2d8` 的字节（detached worktree，`--census-only`），同一判定读
`mismatched_columns=108`（`head_census_exit=1`），逐轴可加；这条 before 面在本轮**用改写后的探针字节
重跑过一遍**，与归档体逐字相同（110 行、108 条 remainder），所以它不依赖旧仪器。

| 轴 | 提供者 | 列数 | 处置 |
| --- | --- | --- | --- |
| kind | federal_reserve `SvenssonYieldCurve` | 99 | `str` → `float`（行类字段是 `float | None`） |
| required/nullability | federal_reserve `FederalFundsRate`/`OvernightBankFundingRate`/`SOFR` 的 `rate` | 3 | `required=True, nullable=False` |
| nullability | cboe `AvailableIndices.symbol`/`IndexConstituents.symbol`/`IndexSearch.symbol`/`IndexSearch.name` | 4 | `nullable=False` |
| nullability | intrinio `HistoricalDividends.ex_dividend_date`/`amount` | 2 | `nullable=False` |

108 = 99 + 3 + 4 + 2，与本轮实际改动的列数逐一对上；加上时态例外的 3 列，早期那台不带例外的
kind 普查读到的 111 也解释完毕（111 = 108 真实分歧 + 3 例外面）。当前面读 `mismatched_columns=0`。

## 4. 13 条反例臂（两向都要能落）

`tradingeconomics::EconomicCalendar.date_span` 是 `Literal[0, 1]`：夹具过去按 kind 发 `'alpha'`，
行类拒收。修法不是放宽判定，而是让夹具**去读行类的域**（新公共函数 `sampled_cell`，与既有
`_row_temporal_type` 同一个先例）。

```
date_span=1 -> ok:date_span=1   date_span=2 -> refused   date_span=3 -> refused   date_span='x' -> refused
SOFR percentRate dropped -> refused:ProviderEngineError  SOFR percentRate null -> refused:ValueTransformError
SOFR percentRate text '4.33' -> ok:rate=0.0433           SOFR percentRate number 4.33 -> refused:ValueTransformError
Svensson BETA0='alpha' -> refused                        Svensson BETA0='ND' -> ok:beta0=None
Svensson BETA0='1.5' -> ok:beta0=1.5                     Svensson BETA0=None -> ok:beta0=None
Svensson BETA0='n.a.' -> refused
```

两条臂纠正了我自己先前写错的期望，值得单独记：

- `rate` 挂着除 100 的 `scale` stage，而这个 stage 按设计**拒绝 JSON 数字**——引擎原话
  `scale by 100: a float would drift; publish it as a string or declare a cast first`。
  所以接受臂必须发文本（`'4.33'` → `0.0433`），而数字臂 `4.33` 被拒是**正向证据**，不是缺陷。
- `beta0` 的契约是 `float | None`，所以 `None` 落在接受列；能证伪 sentinel 的是
  `'n.a.'`——四个已声明 token 之外的文本照样被拒，sentinel 不是「任何垃圾串都变 null」。

## 5. 全量测试面（`runs/suite-three-runs.txt`）

判定用失败**名集合**做差，不依赖分母：8 条转绿、1 条由 alignment 新开并在同一轮内关掉、arm 修复后新开
0 条、31 条仍红（全部落在早已记录为 gap 的家族）。run1 `collected=26467 / 39 failed`；
run2 `collected=26551 / 32 failed`；run3 `collected=26552 / 31 failed`。run2→run3 的 +1 正是本轮新增那条臂，
已在盘上复核为 `12 tests collected`（原 11）。

run1→run2 的 **+84 本轮闭合，且归因到仪器而不是代码**：门禁成员 12 跑同一棵树，配方是
`pytest tests -n 8 -m "not e2e"`（`Makefile:96`），而我的三遍仪器没带 `-m`。两份日志的 SKIPPED 名字集合做差
（run3 `/tmp/c106_suite_after2.txt` 271 条 / 成员 12 `/tmp/c106_gate_chain.txt` 187 条，反方向差集为空），
差出的 **84 条**落在 **22 个文件**里，这 22 个文件**每一个**都带 `@pytest.mark.e2e`
（`tests/test_api_keys.py:526 TestApiKeyRoundTripOnMysql`、`tests/test_data_query_api.py:109
TestAgainstTheWarehouse`），且**没有一个**在本 slice 改过的路径上。算术零残差：`26468+84=26552`、
`187+84=271`、`26551−84=26467`＝run1 分母，两个仪器的 31 条失败名**逐字相同**（排序后 `diff` 读空）。
所以「alignment 扩充了 parametrize」这个说法被**证伪**。副产品一条：成员 12 的红不是覆盖率
（`Total coverage: 84.31%` ≥ floor 84），就是那 31 条已知红。逐名清单在 `runs/suite-three-runs.txt`
末尾的 `mover_84_attribution` 段。

## 6. 本轮新开又本轮关掉的一条（不设口头判定）

alignment 把 `rate` 对齐成 required/non-nullable 之后，`tests/test_federal_reserve_obfr_spec.py` 的
`TestSentinelAbsenceFacet` 那条反例臂**造不出来了**：`_engine/spec.py:451` 直接拒绝
`column 'rate' declares null sentinels ('', "''", 'NA') but is non-nullable: the rewrite produces
exactly the null the row model refuses`。这本身是本轮要的结论——错误声明现在在建模期就被拒，
不再等到 fetch。处置是：见证断言（`rejected == ['rate']`）逐字不动，maim 补上它在对齐前本就带着的
`nullable=True` 那一条腿，并**新增**一条臂去量新的、更早的那层拒绝。该文件 11 items → 12 items，`12 passed`。

## 7. 门禁 17 成员逐条复跑（`runs/gate-members-chain.txt`，原始日志 `/tmp/c106_gate_chain.txt`）

`gate:` 是一条 recipe，成员 1 红就把 2-17 一起 abort，所以按 Makefile 同一顺序把 17 个成员在 shell 里逐条跑，
每条打 `MEMBER_EXIT` 与本地时钟。跑之前先证 `git diff --numstat` 空（index == worktree），并把仓库根那一个
`.coverage`（mtime 07:45，移动时没有活着的进程持有它）整份移到 `/tmp/c106_coverage_aside/`，可逆、未删。

**读数：green=8 / red=9 / not_measured=0**（从链自己的 17 条 `MEMBER_EXIT` 行派生，`8+9=17` 闭合）。
`rc=2` 一律是 make 包住非零 recipe，不是工具自己的判定。9 条红逐条归属：

| 成员 | 归属 |
|---|---|
| brand-check | 4 处 `akshare_web` 全在用户侧未跟踪法务草稿，与 §57.2 同一格；不触碰、不豁免 |
| zero-dep-check | 87 条登记里 1 条 digest 过期（`acceptance_item_probe.py`）；漂移由**已提交的** `b15717b` 造成 |
| evidence-traceability | `68953` 条全为 `#untracked`，`C100 68923 + C101 30 = 68953`；只要原始 receipts 不入库就**必然红**（用户裁定的直接后果），不放宽、不豁免 |
| a2-check | 3 面报红（pinned Gitleaks 快照 / ruff check 历史面 / ruff format `1196+2829+1=4026`），mypy 面**未跑完**、bandit **从未跑**——是我杀的；`exit=2` 不是 a2 的判定 |
| public-api-quality | 57 条 `missing docstring`，分布在 9 个 provider 目录（yfinance 19 / oecd 9 / tmx 8 / imf 6 / fmp 5 / tiingo 3 / benzinga 3 / alpha_vantage 3 / econdb 1），与本 slice 的 5 个代码路径**交集 0**（在 57 行名单上 `grep -cE '_engine/(testing\|spec)\.py\|cboe/specs\.py\|federal_reserve/specs\.py\|intrinio/specs\.py'` 读 `0`） |
| acceptance-probe-check | **不是 GAP 判定，是崩溃**：`symbol_census`（probe:2456）兜 `OSError/UnicodeDecodeError` 却兜不住 `SyntaxError`，而 `NON_API_TREES=('tests/','docs/')`（probe:2390）把 7863 份未跟踪证据副本放进母数，于是 `docs/evidence/C100/…/build_execution_ledger.py:101` 一处解析失败让整遍 `--gate-check` **产出 0 条 item 读数**。本轮已在 worktree 修：崩溃改成计数读数，两个被判面各加一条 `Break` |
| test-cov | 31 条失败名与 suite run3 **逐字相同**；`Required test coverage of 84% reached. Total coverage: 84.31%` ⇒ 红不是覆盖率（见 §5 末尾的 +84 归因） |
| frontend-typecheck | 2 条 TS2345（`frontend/e2e/script-download-backend.spec.ts:311,:502`）；`git status --porcelain -- frontend` 读 `0` 行 ⇒ 与本 slice 无关 |
| frontend-e2e | `RUNNER-UNREADABLE: playwright would not list its plane -> exit 2, not a pass`（`frontend_e2e_plane.py:438`）：`runner_totals` 在 `:391-392` 因 `npx --no-install playwright test --list` 返回非零交回 `None`（`:376-378` 那条 `which(npx)` 为空的支路被排除：`command -v npx` = `/opt/homebrew/bin/npx`）。整成员 1 秒，make 在配方第一行就 abort ⇒ **测量不可读**，不是产品判定 |

仪器自身的缺陷写在档案里不遮盖：链的汇总行打 `green=8` / `red=7`（相加 15 ≠ 17），而它自己的 `MEMBER_EXIT`
行给 `8+9=17`；原因是计数正则 `[a-z-]*` 匹配不到带数字的成员名，`a2-check` 与 `frontend-e2e` 对汇总不可见。
逐成员行是权威，那两条错的汇总行按逐字留在档案（`verbatim: members_red=7`），档案另打
`derived_identity: green(8) + red(9) == measured(17) -> True` 与 `tally_agreement=False` 把不一致显式化；
`/tmp/c106_gate_chain.sh` 已在 run2 前改成 `[a-z0-9-]+` 并加两条派生自证行。

还有一条我自己的读数被同一条链**证伪**并留在记录里：成员 8 的 mypy 面我用仓库自己的 `_run_mypy_target`
实测 5 个 target 得 `1.20 s/target`，投影 `×3658 = 73.4 min`；但成员 11 内部同一遍整树 a2 的两条时钟只差
`1625 s`，即 **≤ 0.44 s/target**，投影被自己的读数打到 1/5 以下——1.2 s 那条是被冷启样本（4.47 s）撑起来的，
缓存热度是本轮未测的机制。被否掉的数不删，档案里 `cost_sample_REJECTED` 与 `derived_cost_bound` 两段并列。

修复字节的验证面（成员 11 那条）：`py_compile=ok`、`ruff check` 首次读 `1 error`（E501 在 `:3548:99`，
就是我新写的披露文本自己撑爆行），重排后 `All checks passed!`；`ruff format --check` 读
`1 file already formatted`；`mypy`（缓存写到 `/tmp`）`Success: no issues found in 1 source file`；
`bandit -c bandit.yaml` 对该文件 `0` 条 Issue；`--self-test` 打印
`130/130 个探针走到了判定，941 条 break 各被施加一次、每条都要求把干净读数打回 gap` 与
`OK: … all 941 counterfact(s) flip a clean reading back to a gap.`，`130` 与成员 4 台账的 `items=130`
是两条独立来源对上同一母数。**两点口径**：那次 `--self-test` 跑在 `519b25167a1a…` 字节上、此后只有那条
E501 重排，交付字节是 `a8cc05ef529a…`，`519b2516` 未留副本 ⇒ 该面当时标为**不可复算**；这一条已被 §7b
在交付字节上的重跑**取代**（run1 的原读数照旧留着，因为它记录的是采集时的事实）。
我的退出码通道也坏过（zsh 用 `$pipestatus[1]`，`${PIPESTATUS[0]}` 读空），判决取自打印的 `OK:` 行而非 rc。

## 7b. 修复字节上的重量面（`runs/member11-repair-and-repin-face.txt`）

§7 那句「该面标为**不可复算**，run2 在交付字节上重量」已经重量完了。交付字节 =
`a8cc05ef529ac37d…93a3cca5`（HEAD `c5db910fc3c6…` 仍是修复前），四组读数：

- **判定面** `--item 'AC-17|07'`：`excluded trees 1 (…build_execution_ledger.py:101)`、
  `widened 0 / 存量 0`、`VERDICT AC-17|07: gap`、`item_exit=0`。**崩溃→计数**这条成立，而 gap 本身
  是产品面（见下条归属），不是崩溃的余影。
- **反例面** `--self-test`（在交付字节上重跑）：`130/130 个探针走到了判定，941 条 break 各被施加一次`、
  `OK: … all 941 counterfact(s) flip a clean reading back to a gap.`、`self_test_exit=0`。
  母数由 `sum(len(p.breaks))` 派生：HEAD 字节 939、交付字节 941，差 **2** 正是本轮新加的两条 `Break`；
  手搓的 `Break(` 字面量 AST 计数读 662，**少 279 条**，因为多数探针从 helper 里造臂——那个数不能用。
- **静态面** `a2_check.py --files`（目标 + 一个本轮未动的 tracked 文件做对照）：两侧
  `ruff check / ruff format --check / mypy / bandit` 全 `ok`，唯一红仍是 pinned Gitleaks 快照扫描，
  两条 run 报的是**同一组 25 个路径**（集合比较 `identical=True`、`only_here=0`、`only_there=0`）。
- **重钉面**：批准内容对面在**新字节**上重量，八个计数器一个都没动（`akshare 144/172`、
  `138/163`、`openbb 95/117`、`78/96`、宽口径 `61`、扫描器多重集 `13→13 identical`），
  反例臂 `3→4`/`13→14` 证明那些「不动」是被能动的计数器量出来的；in-memory re-arm
  `A=1(旧钉 stale) / B=0(新钉) / C=1(全零 digest)`、`REARM_BITES=True`。
  §4.1 先前那条**窄口径 `1→1`** 被本轮自己的仪器作废：同一字段 `load_shape_narrow` 打印 `3→3`，
  `1` 在 `/tmp` 里已无任何 stdout 载体，最近的可见出处是 C89 条款的 `brand_load_lines=1`（另一个谓词、
  另一份字节）。作废的数留在档案里，被修正的是**计数与出处**，不是「没有一行加载外部品牌包」这句结论。

还有一条**入库顺序**上的缺陷，是这一轮从 index 上量出来的：`git show :scripts/quality/acceptance_item_probe.py`
读 `c5db910f…`（修复前）而工作树读 `a8cc05ef…`。那一时刻任何 `git commit` 都会把**崩溃**落进历史、把所有
绿色面留在工作树里，所以重钉必须等「重新 `git add` + `git diff --name-only` 读空」之后再做。
本档案自己的 gitleaks 预扫也入档：`scanned_bytes=16881 findings=0`；第一次取数用
`grep -cE "leaks found"` 读到 `1`，差点把它记成泄漏——gitleaks 打的那行是 `INF no leaks found`，
**模式匹配到了自己的否定式**，泄漏计数必须锚 `WRN leaks found` 或直接数 JSON report。

## 8. 没主张的东西（防止把本轮读数当成别的东西）

- 没主张迭代2 的 32 个 provider 全绿：全量仍有 31 条红，任务 #9/#10/#13/#18/#19/#20/#21/#23 全部挂着。
- 没主张 `-999.99` 已可声明：sentinel 匹配的是强制前的**发布文本**，拼不出数字，仍在 `DECLARED_GAPS`。
- 没给 `build_row_model` 加 required/kind 的声明期守卫（本轮只在普查里量出来）；加守卫要带同一套时态
  例外，属于单独一刀，记在 README 的下一步里。
- 没有触碰 frozen 面（`contract`/`verified`/`approved_delta`/`candidate_sha256`/撤回策略/拒录码）。
- 没主张成员 8 的 mypy / bandit 两面有读数：那两面 run1 未跑完；本节的 a2 判定只用
  `runs/align-probe-static-planes.txt` 的 slice 限断面。
- 没主张成员 14/17 的红由本 slice 造成，也没主张它们已被修掉；成员 17 run1 对它**没有任何产品读数**，
  在 run2 补上 `--list` 的 returncode/stderr 之前，它对谁都不是绿灯。
- 没主张成员 11 翻绿：§7b 给的是「**崩溃→计数**」与判定器自身四面的读数，成员 11 的配方是
  `--gate-check`（`Makefile:115`），那一遍**仍未**在交付字节上重跑；AC-17|07 照旧读 `gap`。
  run2 之前不预告它翻绿，也不预告那 56 条会消失。
- 没主张这条逐成员链等价于 `GATE_EXIT`：它按同一顺序跑同一批配方，但**不会因为谁红而 abort**，
  给的是逐成员读数，不是门禁通过与否的答案。
- 重钉目标 digest 是崩溃修复**之后**的字节 `a8cc05ef529a…`，不是 run1 时的 `c5db910fc3c6…`；
  §4.1 那套「批准内容对面」计数器（见《开发验收记录》§63.4.1）**已在新字节上重量过**（§7b：八个计数器
  全未移动，反例臂会动），in-memory re-arm 也读 `A=1 / B=0 / C=1`。但**钉子本身还没被改**：
  `docs/quality/akshare-reference-allowlist.json` 的工作树字节 == HEAD（`198395ec6d8d…`），那一格仍钉着
  `a33e906a8acb…`。re-arm 是排演，不是已完成；落钉与成员 2 复跑属于 C107。
- 没主张 AC-17|07 那 56 条低于标准的 callable 属于本 slice，也没主张用豁免解决它：owner 普查读
  `56/56` 全在 `quality-source-repairs/fed-wam-two-bucket-stage-20261013/`（一棵未跟踪的上一波草稿树），
  与本 slice 改过的五个代码路径交集为 0。把它请出母数=放宽判定，删掉它=不属于本轮的处置，所以两条都
  留着当**待裁**，本轮只把归属量出来。
- 没有 push 授权；`OPENDATA_ALLOW_LIVE_E2E` 全程未设；原始 suite/gate 日志留在 `/tmp` 未入库。
