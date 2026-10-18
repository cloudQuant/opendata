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
- **本波自建探针的静态面原先不是 0**。把 back-fill 闸门的文件名单从手抄两枚改成 `probes/*.py` 的
  glob 之后，第一次跑就在**已提交的探针**上报红：`mypy_probes_exit=1`（4 个错误，全在
  `tmx_two_rows_prevalidation.py`）与 `bandit_probes_exit=1`（2 枚 B404 + 1 条失效的 `nosec B607`），
  取档 `runs/wave31-step2-backfill_gates_take2_dry_widened_legs_red.txt:19,62`。修完之后五枚探针
  ruff/format/mypy/bandit 全 0，且行为不变由重跑比对证明（§7.15）。这一格只关于本波自建仪器；工厂
  自己的静态面是上面那行，两格不互相担保。
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

提交前的索引面凭据筛、提交后的出厂闸（历史面）复测、以及提交后对本提交 87 枚路径的复扫，见 §8.1
（三把加一把反例臂都已落档，本面写完后执行）。

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

7. **一把我自己写的字节闸造出了 87 条假发现**。§8.1 的复扫第一版要证明"扫描读的工作树字节就是提交
   里的字节"，用的是 `git hash-object --batch-check --stdin-paths` 再按 `<oid> <path>` 解析。这个
   flag 在这台 git 上根本不存在：命令死在 usage，stdout 是空的，于是 `on_disk` 是空字典，87 枚路径
   全部报 MISMATCH，`worktree_vs_commit_blob=0/87` —— 而我那一次直接调用绕过了会检查退出码的
   `_git` helper。它没写成假绿，它写成了假红，但同一个失败模式（子进程的退出码不进读数）两个方向都
   能咬。改法：字节比较交回 git 自己（`git diff --name-only <commit>`，经 `_git`，非零即抛），并加
   一条必须能不同的控制臂——同一把尺子量父提交时必须报出这 87 枚漂移。红色那一份留在档：
   `runs/wave31-step2-committed_payload_scan_run1_misparse.txt`。

8. **我把唯一一份绿色复扫覆盖掉了**。改完文件名规则之后我重跑了那条 argv，目的是确认改名生效——它
   确实生效了，但它写的是同一个路径，于是把 05:14 那一份（87 枚、`findings=0`、控制臂咬住）压成了
   红色重取。差别在于：红色那一份是**真的**（工作树确实动了 `face.md`），可绿色那一份从此不可重取。
   现在它按逐字转录存回 `…_92def7e16.txt`，文件头自己声明它不可重取、以及能从哪儿复现（pin 在
   `92def7e16` 的检出）。教训不是"别重跑"，而是**驱动的输出路径必须由输入参数决定**——我一开始
   写死常量名，才让第二次取数有东西可覆盖；改成带 sha 的文件名之后这个撞车在结构上不可能发生。
9. **一条我自己拼错的复合命令把已 staged 的探针从工作树移走了**。本意只是给红色取档改名，我在同一条
   命令尾部带了 `mv probes/committed_payload_scan.py /tmp/…`。它没丢：文件已在 index 里，
   `git show :<path>` 写回来再 `cmp` 与工作树副本，7133 字节逐字相同。暴露它的是同一行 `ls`——所以
   **改名类命令要把"动了哪些路径"打印出来**，别只看退出码。
10. **bandit 在新探针上是红的，红的不是产品码**。`bandit -c bandit.yaml` 报 B404（`import
    subprocess`，Low/High-confidence）并 `exit=1`；查 `Makefile` 才看清工厂的 bandit 平面是
    `PY_BANDIT := opendata scripts alembic alembic_data`，`docs/evidence` 按设计不在里面，所以我这条
    是自己给自己加的闸。修法按仓库惯例：34 个已发布模块都在**挣得豁免的那一行**上写
    `# nosec B404  # <reason>`（bandit.yaml 明确不设全局 skips）。加上之后
    `bandit_backfill_probes_exit=0`。红色那一次留在
    `runs/wave31-step2-backfill_gates_take1_bandit_red.txt`，绿色的 `backfill_gates.txt` 也一并记下
    "这一腿是自加的、工厂不覆盖本目录"，免得一个 0 被读成工厂的判定。

11. **back-fill 那把闸的文件清单是我手抄的两枚，于是它一直在闸自己对象之外**。
    `w31s2_backfill_gates.py` 的静态四腿写死 `NEW_PROBE = committed_payload_scan.py` 加它自己，
    `mypy_backfill_probes_exit=0`（载档里那句 "2 files" 就是证据）。把清单改成
    `PROBE_FILES = sorted(PROBES.glob("*.py"))` 之后，同一批命令在**同一批已提交的探针**上第一次跑就
    报红：`mypy_probes_exit=1`（红取档 `:19`），4 个错误全在
    `probes/tmx_two_rows_prevalidation.py`（`:189` no-any-return、`:260/:275/:467` attr-defined），
    `bandit_probes_exit=1`（同一档 `:62`），2 枚 B404（`post_commit_history_gate.py:30`、
    `w31s2_run_chain.py:28`——后者是**跑整条验收链路的驱动**，从没被 bandit 看过一眼），外加
    `[tester] WARNING nosec encountered (B607), but no failed test on file …:136`，即 §7.10 那一族的
    镜像：一条**已经失效**的豁免。红色取档留在
    `runs/wave31-step2-backfill_gates_take2_dry_widened_legs_red.txt`。教训跟"仪器要闸自己"是同一句：
    **清单必须是派生出来的**，因为手抄的清单只会在我记得加文件的那天才是完整的。

12. **静态腿的退出码被我丢掉了**：循环里写的是 `text, _code = cmd(...)`，复合判定
    `ok = child_code == 0 and in_process == 0 and drift["exit"] == 0` 只看筛子与 pin-drift。也就是说
    §7.11 那四个 mypy 错误和两枚 B404 就算写进载档，也不会让 `backfill_chain_exit` 动一格——红取档
    里那个 `backfill_chain_exit=1`（`:67`）来自 index-empty 分支自己赋的 `child_code=in_process=1`，
    与静态腿无关。一次"绿色的 back-fill 闸门"里可以同时躺着 4 个类型错误。现在四腿的 exit 收进
    `static_codes`，载档多打一行 `static_legs_failed=<n> [...]` 并并入 `ok`。

13. **`two_forms_agree=True` 那一次比的是我自己赋的两个常量**。新加的 index-empty 分支给
    `child_code = in_process = 1` 之后，那行仍然无条件打印——两个 1 相等，于是它在一个**筛子根本没
    跑**的取档上宣称"两种形态一致"（红取档 `:66`）。改成只在 else 分支打印，NOT_RUN 分支打印
    `two_forms_agree=NOT_MEASURED`。

14. **写装载档之后的那行打印能崩掉判定通道**。`carrier.relative_to(REPO)` 在 `--out` 指向仓外
    （我做红灯复算时用的 `/tmp/...`）时抛 `ValueError`：`write_text` 已经落盘、`backfill_chain_exit=1`
    已经打进正文，然后进程死在展示行上。那一次的 `dry_take_exit=1` 其实是 traceback 给的，不是判定
    给的——方向和 §7.7 相反，同一个错误模式的两面。改成 `is_relative_to` 分支，展示用绝对路径兜底。

15. **`type: ignore` 按住的其实是我自己写错的注解**。`_fetcher_class` 与 `_serve` 的返回标注是裸
    `type`，mypy 因此认为这个类没有任何属性，我用两条 `# type: ignore[attr-defined]` 把 `.row_model`
    的读写压下去。把注解改成 `type[Any]`（探针按 `canonical_model` 反查类，本来就没有静态类型可写）
    之后 mypy 立刻报 `Unused "type: ignore" comment` ×2——**陈旧抑制就是注解错误的显影**。删掉两条
    ignore、并给 `_serve` 的返回值加一个带类型局部变量（那句 `no-any-return` 要的是"我承认这个形状"）
    之后 `mypy_probes_exit=0`（5 files）。行为不变的证明不是这句话，而是把探针重跑一遍逐行比对：我
    先手工 `diff` 预核过一次，旧载档从 `materialized:` 起 53 行、新 stdout 52 行，唯一的差额是链路
    驱动追加的 `two_rows_prevalidation_exit=0`——所以新腿把那一行**按名字排除并打印出来**（不静默丢），
    再加一条控制臂：从旧的一侧少比一行时必须读"不同"。正式复算的取档文件名是
    `runs/wave31-step2-backfill_gates_take2.txt`，`preval_inertness` 那一段在它正文里。

## 8. 载档清单（每条自带 argv 与 `<name>_exit=<digit>`）

`docs/evidence/C131/wave31-step2-tmx-dividends-constituents-20261018/runs/`：
`prevalidation_face.txt`(+`.err` 0 字节), `pin_drift_pre_production.txt`,
`pin_drift_pre_production_rerun.txt`（本步 §7.5 修正后的复测）, `producer.txt`, `recorder.txt`,
`merge_take1.txt`, `merge_take2.txt`, `judge.txt`, `two_rows_prevalidation.txt`（链路内再取的一遍）,
`incomplete_row_guard_check.txt`, `readiness_census.txt`, `residue_axis_census.txt`,
`seam_reachability_census.txt`, `receipt_pin_consistency.txt`, `batch5_fence_dry_census.txt`,
`ruff_check.txt`, `ruff_format.txt`, `mypy.txt`, `bandit.txt`,
`pytest_tmx_and_pin_planes.txt` —— 共 18 枚由链路驱动写出的载档，外加 §2 的 rerun 与本条 prevalidation。

探针 5 枚（`probes/` 下 `*.py` 的全部，由 take2 驱动 glob 得出，不是我抄的名单）：
`tmx_two_rows_prevalidation.py`（本步按 §7.15 改过注解与两条陈旧 ignore；它的"全绿"原先只有
ruff/format/py_compile 三把，mypy 从没跑过它——§7.11）、
`w31s2_run_chain.py`（链路驱动，逐条命令自带 argv 与 exit；§7.11 那枚从没被 bandit 看过的就是它）、
`committed_payload_scan.py`（§8.1 的复扫驱动，import 自带筛子而不是重写谓词；它的红色第一次
取档在 `runs/…committed_payload_scan_run1_misparse.txt`，见 §7.7）、
`w31s2_backfill_gates.py`（本步的复合闸，take2 那一次的对象就是它自己）、
`post_commit_history_gate.py`（§8.2 的历史面驱动）。

凭据三把与反例臂（本面写完后执行，读数在 §8.1）：提交前索引面 staged payload scan、提交后
`make secret-check` 复测、提交后对本提交 87 枚路径的复扫。

`runs/` 里 `.txt` 载档本面写作时实测 **32 枚**（`ls runs/*.txt | wc -l`）：上列 20 枚由链路驱动写出
（18 枚链路命令 + §2 的 rerun + 本条 prevalidation），另 12 枚是 back-fill 与凭据面——
`precommit_staged_scan.txt`、`precommit_staged_scan_final.txt`、`payload_scan_falsification.txt`、
`post_commit_secret_check.txt`、`committed_payload_scan_run1_misparse.txt`（§7.7 的假红）、
`committed_payload_scan_92def7e16.txt`（§8.1 的绿色复扫，逐字转录）、
`committed_payload_scan_92def7e16_rerun_after_face_edit.txt`（同 argv 在 back-fill 之后的红色重取）、
`backfill_gates.txt`、`backfill_gates_take1_bandit_red.txt`（§7.10 的红旗取）、
`committed_payload_scan_a8d7b2122.txt` 与 `post_commit_secret_check_a8d7b2122.txt`（§8.2 的两把收口
面）、`backfill_gates_take2_dry_widened_legs_red.txt`（§7.11–14 那一次红灯复算，写它时 `--out` 指的
是 `/tmp`，搬进 `runs/` 前后 `cmp` 与原件逐字节相同）。take2 的正式复算落盘后这里是 33 枚。
`prevalidation_face.txt.err`（0 字节）按既定裁定不入提交，留在工作树。

## 8.1 凭据：提交前索引面 → 反例臂 → 提交后历史面 → 提交后复扫本提交

本步提交 = `92def7e16`（87 个文件，父提交 `1b158436c`）。只提交到本地：**无 push 授权**。

| 哪一把 | 它扫的对象 | 读数 | 载档 |
|---|---|---|---|
| 提交前索引面 | 当时 staged 的 86 枚路径的工作树字节 | `payload_scan_files=86 unresolved=0 findings=0 rc=0`，`staged_payload_scan_final_exit=0` | `runs/wave31-step2-precommit_staged_scan_final.txt:6,8`；同一 argv 在把这两枚凭据载档 add 进 index 之前还取过一遍：84 枚、`findings=0`（`runs/wave31-step2-precommit_staged_scan.txt:6,8`） |
| 反例臂：它会不会咬 | 从当时 staged 集取前 3 枚路径复制进 mktemp 树（仓库树不写） | 植入臂 `scanned=4 findings=1 rules=['github-pat'] gitleaks_rc=1`；干净臂 `findings=0`；拼接字面量臂 `findings=0`；合计 `arms=3 arms_passed=3 arms_failed=0 PASS=True` | `runs/wave31-step2-payload_scan_falsification.txt:9-12` |
| 提交后历史面（出厂闸） | `gitleaks detect --source . --config .gitleaks.toml --redact`，读 git 历史 | `427 commits scanned.` / `no leaks found`，`secret_scan_check_exit=0`；`git rev-list --count HEAD=427` 与之一致 | `runs/wave31-step2-post_commit_secret_check.txt` |
| 提交后复扫 | 本提交自带的 87 枚路径，清单由 `git diff-tree` 现取，不是我抄的 | `commit_paths=87`，`worktree_drift_vs_commit=0 byte_identical=True`，控制臂 `CONTROL_drift_vs_parent=87 expected=87 bites=True`，两取都 `payload_scan_files=87 payload_scan_findings=0 payload_scan_rc=0`，`chain_committed_payload_scan_exit=0` | `runs/wave31-step2-committed_payload_scan_92def7e16.txt`（绿色那一份的逐字转录，文件头写明它已不可重取；同 argv 在 back-fill 之后的红色重取在 `runs/wave31-step2-committed_payload_scan_92def7e16_rerun_after_face_edit.txt`，读 `worktree_drift_vs_commit=1` 与 `DRIFT …/face.md` 后拒绝跑筛子） |

为什么还要第四把：索引面那一把只能给它当时看见的 86 枚定价——一份筛子不可能把记录它自己的载档放进
输入，86 与 87 的差就是这么来的（那份载档第 4 行自己声明了这点）。复扫把清单的来源换成提交本身，
87 枚于是全都有面对象；而"扫描读的是工作树字节"这句话由 `worktree_drift_vs_commit=0` 担保，再由
控制臂担保这个 0 不是空对空（同一把尺子在父提交上必须量出这 87 枚的漂移，实测正是 87）。

两把历史面/复扫面之间的分工：出厂闸证明**整个历史**里没有可读的凭据形态，复扫证明**这一提交的每一
枚字节的当前版本**在同一规则集下干净。前者的对象是历史，后者的对象是工作树==提交的那 87 份字节。

复扫的时间窗（本步实测出来的，不是设计时想到的）：那道字节闸拿工作树与提交比，所以复扫只有在**提交
之后、且该提交里任何一枚路径被再次编辑之前**才成立。back-fill 改了 `face.md`（87 枚之一），于是同
一条 argv 之后永远只能读红。文件名里带 sha 就是这个意思：每一次复扫写自己那一份，谁也不覆盖谁。

植入的字面量在任何载档里都不出现（LCG 现算，只打印规则 id 与命中数）；载档只打印 env 变量的**名字**。

back-fill 那一提交自己也要过同样的闸，读数在 `runs/wave31-step2-backfill_gates.txt`：
`staged_paths=7` 的两形复算（子进程 argv 与在进程内直调 `main()`）都是
`payload_scan_files=7 findings=0 rc=0`、`two_forms_agree=True`；静态面
`ruff_check_new_probe_exit=0`、`ruff_format_new_probe_exit=0`、`mypy_backfill_probes_exit=0`（2 files）、
`bandit_backfill_probes_exit=0`；pin-drift 那一行由链路驱动自己的 `pin_drift_face()` 现取（import，
不重写），读 `pinned_files=16 stale_files=0 identity_set=5168e1e723999312 drifted_rows=[]` ——
back-fill 没有 stale 判定器要读的任何一枚摘要，plane 1 那 288 不因为补文档而动。
它的出厂闸与复扫只能在该提交之后取，因此落在下一个提交里（本仓一贯的滞后一枚：一份载档不在记录它
自己的输入里，由下一提交的历史面收口）。

## 8.2 back-fill 提交 `a8d7b2122` 的两把收口面

本提交 = `a8d7b2122`（9 个文件，父提交 `92def7e16`；清单由 `git diff-tree` 现取，1 枚 `face.md` +
2 枚探针 `committed_payload_scan.py`、`w31s2_backfill_gates.py` + 6 枚 back-fill/凭据载档）。仍然
**只提交到本地，无 push 授权**。

| 哪一把 | 对象 | 实测读数 | 载档 |
|---|---|---|---|
| 索引面复扫（字节闸在后） | `git diff-tree` 现取的这 9 枚路径 | `commit_paths=9 head_commit=a8d7b2122`，`worktree_drift_vs_commit=0 byte_identical=True`，控制臂 `CONTROL_drift_vs_parent=9 expected=9 bites=True`，两取都 `payload_scan_files=9 payload_scan_unresolved=0 findings=0 rc=0`，`chain_committed_payload_scan_exit=0` | `runs/wave31-step2-committed_payload_scan_a8d7b2122.txt` |
| 历史面（出厂闸） | `gitleaks detect --source .`，读整条历史 | `428 commits scanned.` / `no leaks found`，`info_lines=3 info_lines_matching_no_leaks_found=1`，`commits_scanned=428 rev_list_count_head=428 agreement=True`，`commits_scanned_previous_face=427`→`commits_scanned_delta=1 rev_list_span_prev_to_head=1 delta_matches_span=True`，`secret_scan_check_exit=0`、`chain_post_commit_secret_check_exit=0` | `runs/wave31-step2-post_commit_secret_check_a8d7b2122.txt`（驱动 `probes/post_commit_history_gate.py`） |

历史面那一把为什么不能只抄一句 `no leaks found`：空历史或部分历史同样打印这句话，所以
`commits_scanned` 必须与另一件工具（`git rev-list --count HEAD`）独立数出来的 428 相等；而它对上一代
历史面的增量必须等于两枚载档之间的提交数（实测 1 = 1，用的正是从上一份载档的
`head_commit_at_scan=` 字段反解出来的 `92def7e16`，不是我另抄的 sha）。这两条与"恰好一行
`no leaks found`"一起进 `gate_agreement`，三项任一不成立即 `chain_post_commit_secret_check_exit=1`。
驱动把载档写到进程的最后一步、`write_text` 一次落盘，中途崩就没有半成品文件（§7.6 那一族的复发预防）。

9 与 8 的差同 §8.1 里 86 与 87 的差是同一件事：记录复扫的那份载档不可能出现在复扫的输入里。复扫对象
是**提交自带的字节**，而这次它自带 9 枚、其中 1 枚是复扫载档自己 → 该载档由上面第一行的 9 枚之一
（第二枚凭据载档）与下一提交的历史面覆盖。

本波下一个提交（take2 与本轮 face 修订）自己的谓据，写在这里以便它被反驳而不是被相信：

* `committed_payload_scan.py --commit <下一提交>` 必须在**提交之后、该提交任何一枚路径再次被编辑之前**
  读 `worktree_drift_vs_commit=0`。谁先动了 `face.md` 或 take2 载档，它就读 `≥1` 并打印
  `screen_NOT_RUN` —— 那是设计（§8.1 的时间窗），不是故障，也不许改成忽略漂移。
* `post_commit_history_gate.py --prev-carrier runs/wave31-step2-post_commit_secret_check_a8d7b2122.txt`
  应读 `commits_scanned=429`、`delta=1=rev_list_span_prev_to_head`；若 span 与 delta 不等，红的是这一枚
  提交的归属，不是 gitleaks。
* `w31s2_backfill_gates.py --out runs/wave31-step2-backfill_gates_take2.txt` 的 `pin_drift` 腿必须读
  `stale_files=0` 且 `identity_set=5168e1e723999312`。它现在还把 `static_legs_failed`、
  `preval_inertness_exit` 一起并入 `backfill_chain_exit`（§7.12），所以这一枚 0 比上一代那枚 0 覆盖得
  多；plane 1 的 288 与残留 62 不因文档与仪器而动的这句话，由 take2 载档里那一腿的 `drifted_rows=[]`
  承担，不是由本面承担。

## 8.3 提交 `dca897854` 的两把收口面，与 §8.2 三条谓据的对账

本提交 = `dca897854`（9 个文件：`face.md` + 4 枚探针 + 4 枚载档，父提交 `a8d7b2122`）。仍然**无 push
授权**。§8.2 把三条谓据写成"必须读什么"，现在逐条对账——它们都成立，而且成立的方式是被读出来的：

| §8.2 的那条 | 实测 | 载档 |
|---|---|---|
| 复扫必须在该提交之后、任何一枚路径再被编辑之前读 `worktree_drift_vs_commit=0` | `commit_paths=9 head_commit=dca897854`，`worktree_drift_vs_commit=0 byte_identical=True`，控制臂 `CONTROL_drift_vs_parent=9 expected=9 bites=True`，两取都 `payload_scan_files=9 payload_scan_unresolved=0 findings=0 rc=0`，`chain_committed_payload_scan_exit=0` | `runs/wave31-step2-committed_payload_scan_dca897854.txt` |
| 历史面应读 `commits_scanned=429`、`delta=1=span` | `429 commits scanned.` / `no leaks found`，`commits_scanned=429 rev_list_count_head=429 agreement=True`，`commits_scanned_previous_face=428`，`commits_scanned_delta=1 rev_list_span_prev_to_head=1 delta_matches_span=True`，`secret_scan_check_exit=0`、`chain_post_commit_secret_check_exit=0` | `runs/wave31-step2-post_commit_secret_check_dca897854.txt` |
| take2 的 pin-drift 腿必须读 `stale_files=0` 且身份集不变 | `pin_drift_exit=0`、`pinned_files=16 stale_files=0 rows_with_pins=7 drifted_rows=[]`、`identity_set=5168e1e723999312`、`claims=120`；同一载档另有 `static_legs_failed=0 [none]`、`preval_inertness_exit=0`（`old_body_lines=52 new_body_lines=52 identical=True`、`lines_below=1 excluded=[two_rows_prevalidation_exit=0]`、`CONTROL_drop_one_line_from_old_side_differs=True`）、`staged_paths=8` 两形都 `findings=0 rc=0`、`backfill_chain_exit=0` | `runs/wave31-step2-backfill_gates_take2.txt` |

8 与 9 的差还是 §8.1 那件事：take2 的 staged 那一腿只能给它当时看见的 8 枚定价，第 9 枚正是记录这次
定价的载档自己；上面第一行那把复扫把清单来源换成提交本身，于是 9 枚全都有面对象，而它成立的条件
（工作树==提交）由 `byte_identical=True` 担保。

一条"空读数"的处置记在这里，免得下一个读者把它当成被吞掉的输出：take2 里
`bandit_probes: ` 那一行正文是空的，因为 `bandit -q` 在无问题时**什么都不打印**。我用同一条 argv
直接重跑并重定向到文件核对过，`0` 字节；这一腿的判定是它的退出码 `bandit_probes_exit=0`，不是正文。
`runs/` 里 `.txt` 载档本面写作时实测 35 枚（`ls runs/*.txt | wc -l`），take3 那次复算落盘后是 36 枚。

本面到此只闭合了前两条。第三条我当时写成了 `w31s2_backfill_gates.py --out
runs/wave31-step2-backfill_gates_take3.txt`，**这条 argv 本身是错的**，而且只在被照抄的那一刻才显形：
驱动用的是 `carrier = RUNS / args.out`（`probes/w31s2_backfill_gates.py:240`），`--out` 要的是 `runs/`
**里面**的文件名；带 `runs/` 前缀的串会被二次拼成 `runs/runs/…`，那一层目录不存在，于是 `write_text`
在四条腿全部跑完之后抛 `FileNotFoundError`——**四腿全绿、一枚载档都没落下**。这与 §7.14 同族而方向相
反：那里是载档已经写完、判定通道死在它后面的展示行；这里是判定全部跑完、死在落盘那一步，于是"绿"没有
任何可被引用的对象。控制臂在 `/tmp` 里按同形复现：`runs/x.txt` → `FileNotFoundError: [Errno 2] No such
file or directory: '…/runs/runs/x.txt'`，`x.txt` → 写入 10 字节。take3 是用裸名取的，所以它有载档。

## 8.4 第 3 枚 back-fill 自取的 take3，与"发布的谓据也要被复算"

`a8d7b2122` → `dca897854` → 本枚，step 2 的 back-fill 序列到这里第 3 枚，仍然**无 push 授权**。take3 的
读数逐条在 `runs/wave31-step2-backfill_gates_take3.txt`（3204 字节），行号是那份载档自己的：

| 腿 | 实测 |
|---|---|
| pin-drift | `:6-8` `pin_drift_exit=0`、`identity_set=5168e1e723999312 claims=120`、`pinned_files=16 stale_files=0 rows_with_pins=7 drifted_rows=[] this_steps_rows=['HistoricalDividends', 'IndexConstituents']` |
| 静态四把 | `:3` `probe_files_gated=5`（glob 得出，不是我抄的）；`:10/:12/:16/:17` 四把 exit 全 0，`:11` `5 files already formatted`、`:14` `Success: no issues found in 5 source files`；`:16` 正文空是 `bandit -q` 无问题时不打印（§8.3 末） |
| 探针惰性 | `:21-25` `probe_exit=0 stderr_bytes=0`、`old_body_lines=52 new_body_lines=52 identical=True`、`lines_below=1 excluded=[two_rows_prevalidation_exit=0]`、`CONTROL_drop_one_line_from_old_side_differs=True`、`preval_inertness_exit=0` |
| 索引面筛子 | `:26-31` `staged_paths=3`，两形都 `payload_scan_files=3 payload_scan_roots=3 payload_scan_unresolved=0 payload_scan_findings=0 payload_scan_rc=0`，`two_forms_agree=True` |
| 判定 | `:32 static_legs_failed=0 [none]`、`:33 backfill_chain_exit=0` |

`staged_paths=3` 与本枚实际入存的 5 枚路径之差是两件事叠出来的：take3 自己（它跑时还不存在），以及
take3 之后为 face.md 最终字节再取的那一遍。同一 argv 会以 `--out wave31-step2-backfill_gates_take4.txt`
再落一次盘，好让筛子给**带 §8.4 的** face.md 定价——take3 定价的是它还没有 §8.4 的那一份字节。take4 的
读数不属于本面：本面若再为它改一次字，就又落后一代，与上面同形。处置与 §8.2 的 9-vs-8、§8.3 的 3-vs-4
相同：交给下一枚提交里那把清单来源为提交本身的复扫，本枚的 5 枚路径届时全都有面对象，且成立条件由
`worktree_drift_vs_commit=0 byte_identical=True` 担保。

下一枚提交要面对的三条（谓据按 §8.4 的更正版写，`--out` 一律裸名）：

1. `committed_payload_scan.py --commit <本枚 sha>` 应读 `commit_paths=5`、`worktree_drift_vs_commit=0
   byte_identical=True`，控制臂 `CONTROL_drift_vs_parent=5 expected=5 bites=True`；
2. `post_commit_history_gate.py --prev-carrier
   runs/wave31-step2-post_commit_secret_check_dca897854.txt` 应读 `commits_scanned=430`、
   `commits_scanned_delta=1 rev_list_span_prev_to_head=1 delta_matches_span=True`（前值 429 与 `:22-25`
   在 `runs/wave31-step2-post_commit_secret_check_dca897854.txt`）；
3. `w31s2_backfill_gates.py --out wave31-step2-backfill_gates_take5.txt` 应读 `staged_paths` 等于那一枚
   当时的暂存集、`backfill_chain_exit=0`。

`runs/` 里 `.txt` 载档在本面写作时实测 **36 枚**（`ls runs/*.txt | wc -l`，take3 已在内）。take4、take5
落盘后的数由读它们的那一枚面实测，不在这里预填。


