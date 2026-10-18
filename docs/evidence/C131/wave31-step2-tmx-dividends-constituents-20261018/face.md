# wave-31 step 2 —— `tmx::HistoricalDividends` + `tmx::IndexConstituents` 落地，plane 1 286 → 288/350

日期 2026-10-18（本机）。solo：子代理预算 30/30 在 wave-28 前已用尽。判定链一律用仓库自带的
producer → recorder → merge → judge，探针一律 `import` 自带判定器与自带 runner，不重写谓词。

## 1. 结论面（每一条都有载档行号）

| 读数 | 落地前 | 落地后 | 载档 |
|---|---|---|---|
| plane 1 `offline_acceptance_count` | **286** | **288**（/350） | `runs/wave31-step2-pin_drift_pre_production.txt:8`（pre_chain）、`runs/wave31-step2-judge.txt:5`（post_chain） |
| `status_counts` | INCOMPLETE 27 / ACCEPTED 286 / UNREVIEWED 37 | INCOMPLETE 25 / ACCEPTED 288 / UNREVIEWED 37 | 同上两行 |
| 身份集 | `5168e1e723999312` | `5168e1e723999312`（未动） | `runs/wave31-step2-judge.txt:5` |
| `claim_count` | 1565 | 1565（未动） | 同上 |
| `primary_review_count` | 286 | 288 | 同上 |
| tmx 已验收行 | 5 | 7，且新增的两枚正是本步那两枚 | `...pre_production.txt:10-11`、`...judge.txt:6-8` |
| tmx INCOMPLETE | 19 | 17 | `...pre_production.txt:9`、`...judge.txt:6` |
| 残留总数 | 64 | 62 | `runs/wave31-step2-residue_axis_census.txt:80` |
| 分母自检 | — | `accepted_total=288` + `residual_total=62` = 350 | `runs/wave31-step2-seam_reachability_census.txt:40` |

两枚行各 5 相全部 `exit=0 status=PASS`：`runs/wave31-step2-producer.txt:85-94`（argv 就在同一行里
打印，`--evidence-root docs/evidence/C101` 是判定器自己钉摘要的那条路径）。整片 tmx 分片：
`claims emitted=120 (35 PASS, 0 FAIL, 85 BLOCKED) = 24 row(s) x 5 phases`
（`runs/wave31-step2-producer.txt:125`）。

链路旁证（都是双臂可复算的，不是"跑过了"）：

- recorder `signed=7 refused=0`，并显式记下 `re-signing: dropped 5 earlier review(s)` —— 7 枚签署
  全部落在本代 claims 文档上：`runs/wave31-step2-recorder.txt:4,12`。
- merge 覆盖 27 个 shard，`claims=1565 identities=313 reviews=288 accepted_identities=288`
  （`runs/wave31-step2-merge_take2.txt:32`），两次取的 stable payload 摘要相同：
  `take1=take2=605c09b0ea50a3f9`（同文件 `:34`）。
- receipt-pin 面：1440 份 receipt、20567 个 pin 槽，`stale=0 before_ne_after=0`；强制臂把 1440 个
  目标槽全部改脏并验 `must_equal_targeted=True`；re-arm 臂 `stale=0`。三臂 3/3
  （`runs/wave31-step2-receipt_pin_consistency.txt:5-8`）。
- pytest 四面 `709 passed, 179 skipped`（`runs/wave31-step2-pytest_tmx_and_pin_planes.txt:17`），
  同 argv `--collect-only -q` 实测 **888 collected** = 709+179。这个数字与 step 1 完全相同，是
  "注册两枚见证行不会加宽这四套参数化套件"的直接读数：全仓只有 producer 与 runner 读
  `iter2_claims_fixtures`，没有任何测试枚举它。没有消失的样本。
- 静态面：`ruff_check_exit=0`、`ruff_format_exit=0`（4 files already formatted）、`mypy_exit=0`、
  `bandit_exit=0`。
- batch5 围栏 dry：`matched=31 stale=0 missing=0`，`verdict: REFUSED - no stale pin to re-base`
  （`runs/wave31-step2-batch5_fence_dry_census.txt:9-10`，`exit=1` 是这条 argv 的设计拒绝，不是红旗）。

## 2. 落地动的是什么（2 处产品码 + 5 枚见证项）

`opendata/data/providers/tmx/models/fetchers.py`（新摘要 `sha256:16=efa3e5bb4cf80d3e`，旧
`6968b9ccdf10e1d4`）：两行各补一枚 `row_model` ClassVar 声明——
`HistoricalDividendsFetcher.row_model = TmxHistoricalDividend`、
`IndexConstituentsFetcher.row_model = TmxIndexConstituent`。**没有新增任何守卫**：两枚 fetcher 早已
通过共享的 `_row` 助手（`models/fetchers.py:190`）拒绝坏掉的契约单元格，缺的只是 runner 直发腿要读
的那个声明（`iter2_claims_probe_runner.py:424`）。

`scripts/quality/iter2_witnesses/tmx.py`（新摘要 `sha256:16=bb0ffb640c4a751d`，旧
`7b976e79887012e6`）：CONFIG 从 5 枚到 7 枚，另加散文更正（见 §5、§7）。两枚新项各自只声明一份
响应体，`client_seam` 指向 `opendata.data.providers.tmx.models.client`，`method: "POST"`，
`refusal_code=TMX_BAD_ROW`，tamper 分别落在 `…/dividendHistory/dividends/0/exDate` 与
`…/constituents/0/symbol`。

重签税：本代链路的 pre-production 面实测 `pinned_files=14 stale_files=2 rows_with_pins=5
drifted_rows=[五枚旧行]`（`runs/wave31-step2-pin_drift_pre_production.txt:7`），链路之后
`--stage pin-drift` 复测读 `pinned_files=16 stale_files=0 rows_with_pins=7 drifted_rows=[]`
（`runs/wave31-step2-pin_drift_pre_production_rerun.txt:5`）。同一行里实测的
`rows_without_pins` 恰好是 17 枚，与 judge 的 `tmx_INCOMPLETE=17` 是两把独立仪器对同一总体的两次读
法，二者相等。

## 3. counterfact 与控制臂（为什么这个绿不是自证）

`runs/wave31-step2-prevalidation_face.txt`（35 腿 + 14 组控制臂，`prevalidation_exit=0`，
stderr 0 字节）：

- `CONTROL_SEVEN_SERVED`（`:41`）七枚全部 `runtime_identity=True`，即直发腿产出的实例 `type()` 就是
  域契约类本身；两枚新行各 2 行。
- `CONTROL_DECLARE_REMOVED`（`:42`）两枚各做三边：在内存里删掉 ClassVar → `AttributeError` 且站点
  打印为 `iter2_claims_probe_runner.py:424:_run_direct_with_seam`；`restored_is_saved=True`（用 `is`
  比回来，不是比名字）；恢复后再发 2 行。这就是"声明"与"落地"的因果，而不是同代两件事。
- `CONTROL_TAMPER_BITES` 七枚全部 `match=True`：声明的 tamper 以自己的 `refusal_code` 拒绝，未改的
  控制体照常出票。
- `CONTROL_TAMPER_ONE_CELL` 七枚 `changed_cells=1`，其中五枚路径指向某一列、两枚指向列表长度
  （`filings[length 1->2]`、`getCompanyPriceHistory[length 1->2]`）——把 `tamper_reason` 里"只碰一个
  单元格"这类散文断言变成叶子级计数。
- 分页那一组（`CONTROL_DIVIDEND_PAGE_TERMINAL` / `_PAGINATION_LIVE` / `_SEQUENCE_EXHAUSTED` /
  `_SEEN_DISJUNCT` / `_PAGE_SITES`）：终页载体 `served=2 seam_calls=1`，四页序列载体
  `served=8 seam_calls=4`，两条臂合起来才把"循环停了"与"循环从没跑过"分开；三个 `TMX_BAD_PAGE`
  分支各自落在 `fetchers.py:190/485/487` 三个不同栈帧（`distinct_frames=3`），所以"这是三道独立的门"
  是被打印出来的，不是被断言的。
- `CONTROL_DIVIDEND_WINDOW_BOUND` 同一页在四个窗口下分别打印出票数
  （`unbounded served=2 | end_at_first served=1 | start_at_last served=1 |
  end_before_first refused=WitnessFailure "the offline fetch produced zero rows"`），证明
  `ex_dividend_date` 是 fetcher 两边都比较的列，而不是它照抄的列。
- `CONTROL_CONSTITUENT_KEY` 三枚 `TMX_BAD_ROW`（把自然键里任一列置空都会拒绝）配
  `CONTROL_CONSTITUENT_KEY_NOT_POLICED`：把第二枚成员覆写成第一枚的 `symbol`，两行照发——这条臂就是
  本波自己那条"自然键 = 存在性而非唯一性"的范围声明，`natural_key` 说的是存储身份，mapper 不强制它。

提交前的索引面凭据筛与提交后的出厂闸复测见 §8 之后补的两把（本面写完后执行）。

## 4. 两枚判定器伪影（发布，不动判定器）

`STATIC_VERSUS_RUNTIME`（`runs/wave31-step2-prevalidation_face.txt:54`）把 C128 那台静态仪器的
`_row` 读数与运行时读数并排打印：

| 行 | 静态 `_verdict` | 静态 counts | 运行时 |
|---|---|---|---|
| HistoricalDividends | `QUERY_ONLY` | `{'extract_data': 6}`，raises=13 | 5 相全 PASS，tamper 在 `_row:190` 拒绝 |
| IndexConstituents | `NO_CODE_SITE` | `{}`，raises=7 | 5 相全 PASS，tamper 在 `_row:190` 拒绝 |

原因已在见证模块里写清：静态仪器只统计 mapper 方法**词法内部**的 `raise`，而 tmx 的行级拒绝住在共享
助手 `_row`（`models/fetchers.py:190`）里；同理它的 `is_contract=False` 读成
`ROW_CLASS_NOT_CONTRACT`。两个读数允许不一致，本探针把两边都打印出来，不替读者挑好听的那一个。
不动判定器。

## 5. 对 wave-30 / step-1 预测的更正

wave-30 step 0 把这两枚 tmx 行判成"补声明之后 `_verdict` 仍会读
`NO_CODE_SITE`/`QUERY_ONLY`，所以不可落地"。那个预测的**读数**到今天仍然是仪器打印的读数（见 §4），
错的是它由此推出的结论：落地判据是 runner 的五相 + recorder 签署 + judge 的 `OFFLINE_ACCEPTED`，
不是静态 `_verdict`。见证模块现在把这段写成"预测哪里错了"，而不是删掉。

同一模块原先还写着"没有任何 seam 能 withheld 一个答案"，因此把 `TMX_TRANSPORT_ERROR` 列成不可见证。
`CONTROL_DIVIDEND_SEQUENCE_EXHAUSTED` 证伪了它：一页非终页挂在一页载体上，runner 的序列游标抛
`WitnessFailure`，被 `graphql` 的裸 `except Exception` 吞掉并在 `client.py:153` 以
`TMX_TRANSPORT_ERROR` `from None` 重抛。它仍然不能当某枚项的 tamper（载体一旦能回答所有调用就发不出
这个拒绝，而唯一能发出它的那枚项已经把自己的那次 tamper 花在行级拒绝上），但理由换了，模块按新理由
重写。这条"诊断代价"是产品发现，不是 wave 工作。

## 6. 残留 62 行的行动图与下一波谓据

`runs/wave31-step2-residue_axis_census.txt:80`：`residue=62`，
actions = `add_code_carrying_guard 13 / implement_missing_model 19 / ledger_ruled_out 11 /
new_provider_package 2 / retarget_fetcher_output 2 / shard_entry_needs_harness 15`（嵌套和=62，
arm D `status_drift=[] bucket_drift=[]`），八臂 8/8。
`readiness_census.txt:7`：buckets `NEED_BINDING 32 / NEED_DECLARE 15 / NEED_SHARD_ENTRY 15`，
`sums_to_residue=True`；tmx 还剩 `NEED_BINDING 16 + NEED_DECLARE 1`（那 1 枚是 `EquitySearch`，它的
映射只发布四列源列中的两列，是映射裁决而非见证）。
`incomplete_row_guard_check.txt:81`：`incomplete=25 convertible=0` —— INCOMPLETE 桶里已经没有"补个
守卫就能动"的行，下一批必须从行动图取，而不是从状态桶取。

谓据（下一条落地前给界）：若下一枚是 `government_us::WeatherBulletinDownload`
（`add_code_carrying_guard`，需要新写守卫），plane 1 288 → 289；若走 `ledger_ruled_out` 或
`new_provider_package` 类，plane 1 不动，且那类行的第一把门是台账裁决/新包，不是见证模块。失效条件：
residue 与 accepted 之和离开 350，或身份集不再是 `5168e1e723999312`。

## 7. 我错的部分（保留在档，不改写）

1. **发送预算那句是错的**。见证模块原写"`_carrier_sends` 从载体推导预算"，实测
   `CONTROL_DIVIDEND_PAGE_TERMINAL`：`carrier_answerable=1 sends_granted=3 default_floor=3
   budget_is_carrier=False` —— 函数返回的是 `max(_DEFAULT_SENDS, answerable)`，下限是 3。四页序列
   那枚才是 `4 against 4`。模块第二段已按实测改写。
2. **窗口臂的第一版测的是无界路径**。`_serve()` 重新 `_entry(model)` 把臂里带的 `query` 丢了，于是
   它在声称"按边界只发一行"的地方打印了 `served=2`。改成 entry 级覆盖 + 双臂 `_window_face` 后
   才有 §3 那一行；这条错已写进探针 docstring。
3. **`CONTROL_CONSTITUENT_KEY_NOT_POLICED` 第一版是一条不能不同的臂**：它把第二枚成员覆写成
   `sorted(keys)[0]`，而对这个载体那正是该成员自己的值——一次 no-op，"证明"了容忍却什么都没问。
   改成覆写 `members[0]["symbol"]` 后才真不同。
4. **`_refusal_site` 第一版打印 `<no tmx frame>`**，把真正的拒绝者藏掉了；补回退到最后一个栈帧之后，
   声明臂才点名 runner `:424`、序列耗尽臂才点名 `client.py:153`。
5. **载档里一个字段名把自己说成了谎**。本驱动第一版在 pin-drift 行尾打印
   `newly_landed_rows_not_yet_pinned=['HistoricalDividends', 'IndexConstituents']`，这个名字断言"这两
   行还没有 pin"。它在 pre-production 那一代是真的，但同一个 argv 在链路之后复测时读到的
   `rows_with_pins=7` 直接与它矛盾。已改成实测 `rows_without_pins=[17 枚]`
   （`runs/wave31-step2-pin_drift_pre_production_rerun.txt:5`）。注意：链路里那份
   `pin_drift_pre_production.txt` 仍带旧字段名——它那一代成立，而 pre-production 漂移表一生只能取一次，
   今天重跑它的 argv 得到的是 `stale_files=0` 的那一份。此处按"留错数在档"处理，不追改历史载档。
6. 另外三次工具级失败也留在过程里：两次 Edit 因为我凭记忆重造 `old_string`（其中一次甚至包含我根本
   没写过的 `isinstance` 守卫）而 0 命中；一次 `w.PROVIDER_WITNESSES` AttributeError（见证模块没有这个
   属性，应走 `runner.witnesses.model_entry`）；驱动初版把 `contract_version`（一个不带 path 的裸摘要
   字符串）当成 dict 型 pin 组来走，`TypeError` 直接崩在写载档之前——所以那一次 runs/ 里没有半成品文件。

## 8. 载档清单（每条自带 argv 与 `<name>_exit=<digit>`）

`docs/evidence/C131/wave31-step2-tmx-dividends-constituents-20261018/runs/`：
`prevalidation_face.txt`(+`.err` 0 字节), `pin_drift_pre_production.txt`,
`pin_drift_pre_production_rerun.txt`（本步 §7.5 修正后的复测）, `producer.txt`, `recorder.txt`,
`merge_take1.txt`, `merge_take2.txt`, `judge.txt`, `two_rows_prevalidation.txt`（链路内再取的一遍）,
`incomplete_row_guard_check.txt`, `readiness_census.txt`, `residue_axis_census.txt`,
`seam_reachability_census.txt`, `receipt_pin_consistency.txt`, `batch5_fence_dry_census.txt`,
`ruff_check.txt`, `ruff_format.txt`, `mypy.txt`, `bandit.txt`,
`pytest_tmx_and_pin_planes.txt` —— 共 18 枚由链路驱动写出的载档，外加 §2 的 rerun 与本条 prevalidation。

探针：`probes/tmx_two_rows_prevalidation.py`（ruff/format/py_compile 全绿）、
`probes/w31s2_run_chain.py`（链路驱动，逐条命令自带 argv 与 exit）。

凭据两把（本面写完后执行，结果补进 §8.1）：提交前索引面 staged payload scan、提交后
`make secret-check` 复测。
