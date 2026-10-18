# C129 —— runner 的受控假客户端加宽 `request` 动词，deribit 5 行落地并付一次全量重签（迭代2 / 波 29）

日期：2026-10-18。判定口径：仓库自带 judge（`scripts/quality/iter2_model_evidence.py`）在
`docs/evidence/C101/claims.merged.json` 上的 350 恒等式读数，不是任何手算子集。
本波面：**plane 1 276 → 281/350**，残留 69 = INCOMPLETE 8 + UNREVIEWED 61；
冻结信号 `identity_set_sha256[0:16] = 5168e1e723999312` 未动（本轮四个根都读了它，见 §4 臂 B）。

## 1. 本波改了什么（一张写集表）

| 文件 | 改动 | 为什么必须是它 |
| --- | --- | --- |
| [scripts/quality/iter2_claims_probe_runner.py](file:///Users/yunjinqi/Documents/new_projects/opendata/scripts/quality/iter2_claims_probe_runner.py) | +93 / −1；`_FakeSharedClient` 从只服务 `get` 加宽到服务 `request` | deribit 的 fetcher 走 JSON-RPC 的 `client.request(...)`，波前的假客户端根本没有这个方法，五相里 `direct`/`service` 直接 BLOCKED |
| [opendata/data/providers/deribit/models/fetchers.py](file:///Users/yunjinqi/Documents/new_projects/opendata/opendata/data/providers/deribit/models/fetchers.py) | +5 行 `row_model: ClassVar` 声明（[:273](file:///Users/yunjinqi/Documents/new_projects/opendata/opendata/data/providers/deribit/models/fetchers.py#L273)、[:385](file:///Users/yunjinqi/Documents/new_projects/opendata/opendata/data/providers/deribit/models/fetchers.py#L385)、[:428](file:///Users/yunjinqi/Documents/new_projects/opendata/opendata/data/providers/deribit/models/fetchers.py#L428)、[:470](file:///Users/yunjinqi/Documents/new_projects/opendata/opendata/data/providers/deribit/models/fetchers.py#L470)、[:563](file:///Users/yunjinqi/Documents/new_projects/opendata/opendata/data/providers/deribit/models/fetchers.py#L563)） | runner 的 `direct`/`service`/`contract` 三相要求 `type(row) is fetcher_type.row_model`；不声明就没有一行能过 |
| [scripts/quality/iter2_witnesses/deribit.py](file:///Users/yunjinqi/Documents/new_projects/opendata/scripts/quality/iter2_witnesses/deribit.py) | 本仓该 provider 的第一枚见证模块（21078 字节），5 行 × 5 相 + 每行一条 tamper | 见证面；`client_seam` 命名的是加宽后的那条缝 |

runner 侧新落的可调用面（实测行号）：模块级
[`_echo_request_id`](file:///Users/yunjinqi/Documents/new_projects/opendata/scripts/quality/iter2_claims_probe_runner.py#L195)
（JSON-RPC 的 id 回显），类内
[`_method_matches`](file:///Users/yunjinqi/Documents/new_projects/opendata/scripts/quality/iter2_claims_probe_runner.py#L254)、
[`request`](file:///Users/yunjinqi/Documents/new_projects/opendata/scripts/quality/iter2_claims_probe_runner.py#L274)、
[`_respond`](file:///Users/yunjinqi/Documents/new_projects/opendata/scripts/quality/iter2_claims_probe_runner.py#L291)。
加宽不是"给 deribit 开后门"：`_method_matches` 仍按分片声明的 method 过滤，
未声明 `request` 的 provider 走不到这条路径 —— 这一点由 step 1 的 22 臂奇偶面证明
（`fake_client_verb_parity_face arms=22 arms_passed=22 head_sha16=6f73b11a14b8c81d
live_sha16=18b46adc3ccbcfb7 changed_lines=93 parity_verdict=GREEN`）。
但加宽本身不落地任何一行：step 0 的反事实普查对 29 行的潜在人群打的是
`convertible=0 convertible_rows=[] movable=True`，deribit 五行当时的读数也是
`declare_tag=NEEDS_RETARGET verdict=ROW_CLASS_NOT_CONTRACT witness_module=False`。
本波真正落这五行用的是三件事合起来：缝（runner 加宽）+ 声明（5 枚 `row_model` 指向契约类）
+ 分片（第一枚 deribit 见证模块）。少任何一件，plane 1 一行也不会动。

## 2. 为什么一次共享仪器编辑要付 25 份重生产 + 26 份重签

这是本波最贵的一步，钱必须说清去哪了：

1. judge 会把每条 claim 的每个 pin **重新哈希与盘面对比**。命中不一致就在
   [iter2_model_evidence.py:623-627](file:///Users/yunjinqi/Documents/new_projects/opendata/scripts/quality/iter2_model_evidence.py#L623-L627)
   追加 `{pin}_current_hash_drift` 并置 `stale`，
   [:724-726](file:///Users/yunjinqi/Documents/new_projects/opendata/scripts/quality/iter2_model_evidence.py#L724-L726)
   调 `_validate_pin_groups`，
   [:731](file:///Users/yunjinqi/Documents/new_projects/opendata/scripts/quality/iter2_model_evidence.py#L731)
   的 `valid = not issues and not stale` 决定这一行还成立不成立。
   共享 runner 正是所有 claim 的 `probe` pin 的对象 ⇒ 只改 runner 不重生产，
   276 条已接受行会同时降级。
2. 重生产又作废 review：recorder 在
   [iter2_primary_review_recorder.py:163](file:///Users/yunjinqi/Documents/new_projects/opendata/scripts/quality/iter2_primary_review_recorder.py#L163)
   把 review 钉在 `sha256_bytes(claims_bytes)` 上，并在
   [:285-300](file:///Users/yunjinqi/Documents/new_projects/opendata/scripts/quality/iter2_primary_review_recorder.py#L285-L300)
   对有问题的 claim 拒签、
   [:329](file:///Users/yunjinqi/Documents/new_projects/opendata/scripts/quality/iter2_primary_review_recorder.py#L329)
   依 identity 丢弃并替换旧 review ⇒ claims.json 一改，全部 review 必须重写。

所以链条是 producer(25 家) → 不变量探针 → recorder(26 家) → merge → judge → 复算探针。
**禁止的捷径**是用 recorder 的 `--allow-unproven` 只刷 pin 而不真的重跑见证；
那正是目标里"禁止口头判定"要拦的形状，本波没有用。

## 3. 链条逐环读数（每环的 face 行原样取自各自载档）

```
2c reproduce_all_shards   shards=25 clean=20 red=5(旧谓词) emitted=1420 pass=1380 blocked=40
2e invariance_delta       shards=26 arms=7 moved=0 signable=281 verdict=GREEN
2f resign_all_shards      shards=26 clean=26 red=0 signed_total=281 expected_total=281 refused=8
2g merge                  claims=1445 identities=289 reviews=281 accepted_identities=281
2g judge                  350 identities, 281 offline accepted
                          status_counts={INCOMPLETE:8, OFFLINE_ACCEPTED:281, UNREVIEWED:61}
                          global_issues=[] invalid_claims=0
2h verify_merge_judge     arms=7 arms_passed=7 accepted=281 identity_set=5168e1e723999312 GREEN
```

`deribit` 五行全部 `OFFLINE_ACCEPTED`（`FuturesHistorical / FuturesInfo / FuturesInstruments /
FuturesCurve / OptionsChains`），每行五相 claim `status=PASS`，25 条 claim 全部
`exit=0`（step 2b 的 pre-validation 载档逐条打了 `VERDICT=PASS`）。

### 3.1 第二代：用「当前字节」把四环再跑一遍（step 2f2 / 2g2 / 2h2 / 2j）

上面那四环的 resign 一步，载档与脚本后来不同代了：`resign_all_shards.py` 在
07:30:38 被 `ruff format` 改过排版，而 step 2f 的载档产于 07:23:55（§5 缺陷 5）。
补救不是改记录，是用盘上现在的字节重跑，并把两代并排读：

```
2f2 resign      shards=26 clean=26 red=0 signed_total=281 expected_total=281 refused_total=8
2g2 merge       claims=1445 identities=289 reviews=281 accepted_identities=281
2g2 judge       350 identities, 281 offline accepted（与 2g 同一行）
2h2 verify      arms=7 arms_passed=7 accepted=281 identity_set=5168e1e723999312 GREEN
2j  post_format arms=7 arms_passed=7 base=99834046c2db verdict=GREEN   （第一代，七臂）
2j2 post_format arms=9 arms_passed=9 base=99834046c2db verdict=GREEN   （定稿这代，加 H/I）
```

`2j` 与 `2j2` 是同一支探针的两代字节：定稿前我把 §6 第四个事实与 §8 的分族普查从散文升级成臂 H/I，
所以这台仪器自己也要重新跑一遍并重新归位（§5 缺陷 5 立的规矩）。两代载档的**共 7 臂逐字相同**
（`shared=7 shared_identical=True new_only=['H_…','I_…'] dropped_arms=[]`，两份载档按臂名对齐现读），
`post_format_two_generation_face` 那行只有 `arms=` 从 7 变 9 —— 加臂不改判定，这与 §4 的 A–G 同一口径。
加臂过程中我自己红了两回，都写进了 §5 缺陷 8/9，并且都改在仪器里而不是改在散文里。

等值判定由 `probes/post_format_two_generation_face.py` 做，它读的是仓库里两代载档的 face 行与
逐 provider 账（A/B/C 三臂），不是任何手抄数字。另两条臂值得单独说：
D 臂读盘上 judge 报告的 `claims_input.sha256` 等于此刻 merge 的字节摘要，且
`disk_accepted == signed_total == 281`；E 臂把波 29 的落面对齐到**钉死 full sha 的波前提交**
`99834046c2db5eba1d39e9c8e176e0699a76a50c`（用 `HEAD~n` 会在本波提交后悄悄换对象），
读回 `population=350 accepted=276->281`、
`shapes={'UNREVIEWED->OFFLINE_ACCEPTED': 5, 'UNREVIEWED->INCOMPLETE': 2}`、
gained 五枚全在 deribit、incomplete 两枚全在 government_us。
F 臂两处伪造各自转红（把新代 `signed_total` 改成 280 ⇒ A 红；把波前报告里一枚 deribit 行伪造成
已接受 ⇒ E 读到 gained=4 而红），G 臂撤销后回绿。

字段级差分（上一代的 merge/report 快照只存在于主线 `/tmp`，不在版本库里，因此这块不可复算，
只能作为转录留在 step 2j 载档的第二块）：281 条 review 里动过的叶只有 `reviewed_at_utc`
与 `receipt.sha256` 两格，350 个恒等式的 status 一字不差，报告剥掉时间戳与 `claims_input`
后整文档相等。`receipt.sha256` 跟着动是派生的：
[iter2_primary_review_recorder.py:172](file:///Users/yunjinqi/Documents/new_projects/opendata/scripts/quality/iter2_primary_review_recorder.py#L172)
把含 `reviewed_at_utc` 的收据正文序列化，
[:187](file:///Users/yunjinqi/Documents/new_projects/opendata/scripts/quality/iter2_primary_review_recorder.py#L187)
把那段正文整段哈希成 `receipt.sha256`。

## 4. 复算仪器（`probes/verify_merge_judge_face.py`，7 臂，sha16 `b46d1e51fd260078`）

这支探针不复制判定语句：它 `sys.path` 指到 `scripts/quality/` 后
`import iter2_model_evidence`，用 shipped CLI 调的同一对函数（`load_inventory`、`build_report`）
在内存里把 plane 1 再算一遍。

* **A_shards_join_the_merge**：`shards=26 merged_claims=1445 verdict_moves=0` ——
  26 份 reviewed 分片的并集逐键等于 merge，且 recorder 没有改动 producer 写下的任何一格 status。
* **B_identity_set_still_pinned**：CSV / merge 文档 / judge 报告 / `EXPECTED_IDENTITY_SHA256`
  四个根相等；`report_models=350`。
* **C_judge_recomputes_the_archived_report**：`rerun == archived` 是**整份文档**相等，
  且 `claims_input.sha256` 等于此刻 merge 的字节摘要 ⇒ 载档里那份报告描述的是盘上现在这份 merge。
* **D_status_partition**：逐 model 重算的 Counter 等于发布的 `status_counts`，相加 350。
* **E_accepted_equals_reviews_equals_prediction**：
  `accepted(集合) == review_keys == 五相全 PASS 的集合`，长度 281 == merge 的 review 条数 ==
  step 2f 载档 face 行的 `signed_total`（载档是被正则读回的，见 §5 缺陷 1）。
* **F_no_pin_claims_equal_the_produced_blocked**：merge 里 `pins.probe == []` 的 40 条
  全是 BLOCKED、全 `exit_code=3`，与 step 2c 载档 25 行分片账的 `blocked=` 求和 40 相等。
* **G_dropping_a_review_moves_the_count**：深拷贝里删掉排序后第一枚已接受 review，
  重跑得 `control_accepted=280 control_status=INCOMPLETE`。掉不下来，上面四臂读到的 281
  就只是抄来的常数。

## 5. 一处真实发现 + 十一处仪表缺陷（都留字，不抹平）

**发现：HEAD 的 `government_us/claims.json` 相对 producer 自己的人口是欠员的。**
CSV 里该 provider 有 6 行，HEAD 分片只有 4 个 identity。本轮重生产按 producer 的规则补成
5 相全 BLOCKED，judge 于是把 `TreasuryPrices`、`WeatherBulletinDownload` 两行从 UNREVIEWED
改记 INCOMPLETE。这解释了 plane 1 的减项：`OFFLINE_ACCEPTED +5`（deribit 五行）、
`UNREVIEWED −7`（5 行转 ACCEPT + 2 行补了 claim）、`INCOMPLETE +2`（正是那两行）。
这两行仍未验收，只是从"没有任何 claim"变成"有 claim，且 claim 说没配见证"。
这段减法不是手算的：step 2j 的 E 臂把钉死的波前提交与盘面报告按恒等式对齐，
`shapes` 那一行就是它读出来的（载档 `runs/wave29-step2j-post_format_two_generation_face.txt`）。

1. `reproduce_all_shards.py` 的 `bad` 谓词把 `blocked != 0` 也算红 ⇒ 25 个 `exit=0` 的分片里
   5 个被记成红。BLOCKED 是 producer 对跑不起来的相的合法判定
   （[iter2_claims_producer.py:217](file:///Users/yunjinqi/Documents/new_projects/opendata/scripts/quality/iter2_claims_producer.py#L217)、
   [:361](file:///Users/yunjinqi/Documents/new_projects/opendata/scripts/quality/iter2_claims_producer.py#L361)）。
   谓词已改为只把 `exit != 0`、`failed`、解析失败算红，face 另打
   `claims_failed`/`claims_blocked`/`blocked_providers`。**已归档那份 raw log 保留旧 face 行**，
   由不变量探针 arm E 用驱动自己打的逐分片账读回真值。
2. `resign_all_shards.py` 原把 `refused != 0` 算红；跑之前换成"分片 `signed` 必须等于从盘上
   claims 推出的 all-PASS identity 数"，并把 `expected_total` 与 `refused_total` 并排打出来。
   于是 281 这一次是**两个独立人群计数相遇**（step 2e 的 `all_pass=281` 与 step 2f 的实测），
   而不是同一个数被抄了两次。
3. `verify_merge_judge_face.py` 自己的两代红：arm E 最初用无锚正则在整个载档搜 `signed_total`，
   命中 face 行与 prose 行各一次并求和，打出 562；arm C 最初用绝对路径 `load_inventory`，
   `inventory["path"]` 根不同导致整文档比较失败。改法是锚定 `^resign_face` 并要求恰好一个命中、
   以及 `os.chdir(REPO_ROOT)` 后按 shipped argv 的相对路径读取 —— 把"读哪一行/哪个根"说清楚，
   而不是把比较缩小到 summary。
4. step 3 的第一块取档用了 bash 的 `${PIPESTATUS[0]}`，zsh 下展开为空并兜到 `tail` 的返回码，
   于是 `ruff_check_exit=0` 与它旁边的 `Found 440 errors` 自相矛盾。这块原样留在载档里，
   第二块逐命令单独取 exit 重取。
5. §9 那张表当初把 `resign_all_shards.py` 记成 `067fb9b23229cc7f`，而盘上是 `f0f3ed96c2734ec3`：
   差的那一次就是 step 3 里那次 `ruff format`。摘要写的是"脚本改排版之前"的那一代字节，
   而载档记的是"脚本改排版之后"的读数——两句都不算错，但没有一句能被复算。
   这就是 §3.1 那次重跑存在的原因；表里现在两格都留（旧值注明是那一次 format 之前）。
6. step 3 那块载档打了 pytest 的计数，却没打它的 argv，"1412 passed, 25136 deselected"
   这个选择面现在无法复现。step 5 的载档因此把每条命令的 argv 打在块标题里。
   同一轮里我自己的两条仪表缺陷也被顺手量出来了：把 14 枚文件名塞进一个不加引号的 shell 变量，
   zsh 不做词分割、整串当一个路径传给 ruff，读出两条假红（"File name too long"）；
   按目录数 ruff 违规时第一版用默认图形输出配行首目录名锚点，五个目录全读 0（假零），
   换成 `--output-format=concise` 才读到 110/13/34/0/0 = 157。
7. §8 第一稿把"61 行里最大的一簇"写成 tmx 的 24 行。这是**抽查冒充普查**：那个数来自我本轮一直在读的
   step 0 反事实表（那 24 行确实逐行打了），而 `UNREVIEWED` 的按 provider 分布我直到定稿前才现数 ——
   现数读的是 `yfinance 29`，比 tmx 大 5。更正不是文字修饰：它改的是下一波的起点（yfinance 那 29 行
   从来没跑过 producer，`docs/evidence/C101/yfinance/` 整个目录不存在，而 tmx 缺的是绑定注册而不是分片）。
   规则收严一句：**任何"N 行里最大的一簇是 X"的声称必须由同一轮把 N 按同一个键切完并让分桶相加等于 N 的
   读数来携带**，否则它只是我记忆里那张贴过标签的待办清单。
8. 补上这条普查时，我第一稿把 H 臂的 provider **总体**从判定报告自己的 `models` 列表里取。后果在
   I 臂里现形：伪造把 yfinance 的行改标成 wsj，yfinance 同时从分子与分母里消失，集合照样相等，
   `sets_equal` 仍是 True，而臂报 PASS —— 载档里会出现 `sets_broke=False(want True)` 与 `PASS` 并排
   的自相矛盾行。修法是把总体换成 shipped judge 的 `load_inventory` 现读 CSV 的 provider 列
   （顺带把 `csv_sha256` 与报告 `inventory.csv_sha256` 对照，就是 I 臂的 `csv_sha16_matches_report=True`）。
   **分母不许来自它所要计数的那个文档** —— 这是 §95 那条"部分/整体同一性"在本波的第二种形状。
9. 第一次的伪造只改**一**行（yfinance 还剩 28 行，名字两边都在）。那样红只落在
   "最大簇 = yfinance 29"这一格，H 声称的 partition 关系根本没被扳到 —— 控制臂扳不动它声称要扳的东西，
   这条 GREEN 就是空的。改成整簇 29 行一起改标：桶 61、人数 350 都不动，集合两边各自断开，
   I 才真正测到 H 读的是关系而不是数。
10. step 5 到定稿之间 traceability 总数从 69142 涨到 69148，我想用 `st_birthtime` 把这些增量归因到
    具体文件，路数本身被自己的对照否证了（读数在 `runs/wave29-step6-static_planes_final.txt`）：
    5 条"step 5 之后出生"的名单里，`face.md` 与这台两代探针**都在 step 5 那代就在册**（2j 载档出生
    07:48:10 早于 step 5 载档 07:51:40，而它正文点名了探针路径），可它们现在的 birthtime 分别是
    08:20:50 / 08:02:38。原因是编辑走「临时文件 + 改名覆盖」，换 inode 连 birthtime 一起刷新，
    **birthtime 也不等于创建**。我第一版把"mtime 会被编辑刷新、birthtime 不会"当成理由写了进去，
    这句是错的，且错得可测 —— 所以载档把否证它的对照一起留着，而不是只留活下来的那个数。
    根因还是 step 5 只归档了总数没归档名单：**总数差无法逐项归因，只有名单差可以**。
    从这一代起非 C100 的 101 行逐字落盘，下一轮做跨代差集就有基准了。

11. **静态面里有一枚活写手，它会给自己涨母数。** 我把两条 pytest 加进 step 6 的 builder 并把 traceability
    放在它们之前，本来只是为了"别让载档给自己充气"；跑完才发现 C100 那一格从 69045 涨到 69047 的来源
    就是上一代 builder 的 pytest：`tests/test_script_download_backend_iter2.py` 每次执行都在
    `docs/evidence/C100/frontend-download/runs/` 下新建一枚时间戳目录并落两份 JSON，而 traceability 按
    文件系统普查 `docs/evidence/`。这条 `+2/次` 让"档案未变 ⇒ 母数不变"这个默认假设直接失效 ——
    以前我把 traceability 的总数当成档案的纯函数来对比跨代读数，那个假设是错的。
    现在改成两条能测的规则：**(a)** 同一代里先取母数、后跑会写盘的命令；**(b)** 任何跨代差必须先按
    目录点名写手（`frontend_download_runs_dirs_total=55 dirs_born_after_step5=1 fd_charges_those_dirs=2`），
    再谈剩余。归因不出的那 3 条留成 `residual_unexplained=3`，不靠散文补平。

## 6. judge 的桶语义（本波量出来的，以前只按名字猜）

`UNREVIEWED` 描述的是**没有 claim** 的人群：61 行全部 `missing_phases` 五相全缺。
`INCOMPLETE` 描述的是**有 claim 但不成立**的人群：8 行全部 `missing_phases=0`。
臂 G 顺带钉住第三个事实：一行五相全 PASS 而 review 被摘掉时落进的是 INCOMPLETE，不是 UNREVIEWED。
所以"补 claim"会让一行从 UNREVIEWED 迁到 INCOMPLETE —— 状态变差不是回归，是描述变准。

第四个事实是定稿前的普查补上的，因为它把 `UNREVIEWED` 从"61 行散缺"改写成"6 家整缺席"：
CSV 的 32 家里有 26 家有 `docs/evidence/C101/<provider>/claims.json`，缺席的 6 家
（biztoc / seeking_alpha / stockgrid / tmx / wsj / yfinance）按 provider 切完正好是这 61 行
（29 + 24 + 3 + 3 + 1 + 1 = 61，相加等于 `status_counts['UNREVIEWED']`）。
读数由同一次循环同时给出 `shards_present=26`、`shards_absent=6`、`26+6=32`，
分桶相加等于桶大小 ⇒ 这一格不是从两本账各抄一半拼出来的。
`yfinance` 29 行与 `tmx` 24 行是各自 provider 的**全部**行数，所以这两家在 plane 1 里的当前贡献是 0。
**这一段住在载档里而不是散文里**：`runs/wave29-step2j2-post_format_two_generation_face_9arms.txt`
的 H 臂把缺席名单与 UNREVIEWED 名单并排印出（`sets_equal=True`），I 臂用保计数的整簇改标证明它读的是
关系而不是 61 这个数。

## 7. 静态面与留下的 mover

`runs/wave29-step3-static_planes.txt`：本波写集三枚文件
`ruff_check_touched_exit=0`、`ruff_format_check_touched_exit=0`（十枚，含四枚探针）、
`mypy_exit=0` = `Success: no issues found in 3 source files`。
切片测试 `2 failed, 1412 passed, 25136 deselected`：两枚红都用两相控制归为波前既有
（"外来路径 0 行 / 写集 3 行"：`git status --porcelain` 对
`tests/test_congress_cboe_iter2.py`、`tests/test_script_download_backend_iter2.py`、
`opendata/data/models/` 读 0 行，对 `opendata scripts` 读 3 行，空读数必须有非空那一侧撑着）。
第一枚是测试夹具把 `number='42'` 喂给 `CongressAmendment.number: int`，
其契约类在 `opendata/data/models/congress_cboe_iter2.py:130`，本波未动；
第二枚是 `DockerCommandError` 的环境门。
`ruff check .` 的 440 = `PY_SELFDEV` 157（opendata 110 / tests 34 / scripts 13）+ 未跟踪的
`quality-source-repairs/` 283。**157 是留下的未解 mover**：`make gate` 自波 20 起没跑过，
本波 commit 不声称 gate 绿，只声称写集自身三枚静态面绿。

### 7.1 step 5：写集扩到 14 枚之后的静态面（载档 `runs/wave29-step5-static_planes_retake.txt`）

每条命令的 argv 打在块标题里，exit 行逐命令单独取（这是为了补 §5 缺陷 6 那条"读数没有配方"）：

```
ruff check（14 枚）                    ruff_check_touched_exit=0        All checks passed!
ruff format --check（同 14 枚）         ruff_format_check_touched_exit=0  14 files already formatted
mypy（3 枚产品文件）                    mypy_exit=0                      no issues found in 3 source files
pytest -k deribit                       pytest_k_deribit_exit=0          51 passed, 26499 deselected
pytest（两枚波前红文件）                 pytest_two_red_files_exit=1      2 failed, 54 passed
ruff check --output-format=concise .    ruff_check_whole_tree_exit=1     Found 440 errors
evidence_traceability                   traceability_exit=1              69142 new violations
```

整树 440 的按目录拆分这次是锚定读出来的：opendata=110、scripts=13、tests=34、alembic=0、
alembic_data=0（⇒ PY_SELFDEV 157）+ quality-source-repairs=283；余下 2 行是汇总行与提示行，
不是 finding。`evidence_traceability` 那 69142 条按规则只有两种：untracked 69141 与
narrative 1（`docs/evidence/C108/`），**exit 规则 0 条** —— 这一格才是本波关心的：波里每一份
载档（含新落的 2f2/2g2/2h2/2j）都带着自己的直接返回码行。69141 条 untracked 里 69043 是
C100 的原始收据（裁决"只提交代码 + 精选证据"留在盘上），这台仪器走文件系统不走 git 索引，
所以 `.gitignore` 不会让它变小 —— 与 §7 主块那条 157 一起，是两个本波没动的 open mover。

两句要按 §7.2 更正：`69043 是 C100` 这个拆分在 step 5 那代**没有载档**（那块只打了总数），
step 6 现读的是 `by_root` 里的 **69047**；而 step 5 之后写集又动过两次字节、静态面里那格 pytest 又落了两枚
收据（§5 缺陷 11），所以它自己的总数 69142 也已被 69148 取代。旧值留着不抹平，是为了让
"总数差不可逐项归因"这条有实物可指。

### 7.2 step 6：加臂与新增载档之后的最终代静态面（载档 `runs/wave29-step6-static_planes_final.txt`，214 行 / 20304 字节 / sha16 `5024bf7506d198ce`）

step 5 之后本波还动了两次字节（给两代等值探针加 H/I 两臂、落 2j2 那份载档），所以静态面要在
**最终代字节**上重取一次才算这一波的收尾。这台 builder 把 14 枚写集逐字打印（step 5 载档把它缩写成
`<14 files>`，那正是 step 5 想修的「读数没有配方」留在文件清单那一层），每条命令的 exit 由
`subprocess.run` 的返回码转写，不经管道：

```
ruff check（14 枚）                    ruff_check_touched_exit=0         All checks passed!（stderr 0 字节）
ruff format --check（同 14 枚）         ruff_format_check_touched_exit=0   14 files already formatted
mypy（3 枚产品文件）                    mypy_exit=0                        no issues found in 3 source files
ruff check --output-format=concise .    ruff_check_whole_tree_exit=1       Found 440 errors
evidence_traceability                   traceability_exit=1                69148 new violations
两代等值探针（跑两趟）                   two_gen_face_exit=0 run1/2_exit=0  arms=9 arms_passed=9 verdict=GREEN
pytest -k deribit（排在 traceability 之后） pytest_k_deribit_exit=0           51 passed, 26499 deselected
pytest 两枚波前红文件（同后）           pytest_two_red_files_exit=1        2 failed, 54 passed
```

`Found 440` 与按目录拆分同代自洽：`py_selfdev_by_dir={"opendata":110,"scripts":13,"tests":34}`、
`py_selfdev_sum=157`、`quality_source_repairs=283`（157 + 283 = 440）。traceability 这代给了两条
**互相独立的分割**去钉同一个总数：`by_rule` 相加 69148、`by_root` 相加 69148，两本账对得上才写
`rule_rows_sum=69148 root_rows_sum=69148`；规则侧仍只有 `untracked 69147 + narrative 1`，
**exit 规则 0 条**（这一格从 step 5 到 step 6 没动，是本波真正在守的那条）。根侧现读
C100 69047 / C101 62 / C129 26 / C108 12 / C128 1，非 C100 的 101 行逐字名单落盘。

探针那两趟把 §3.1 里「stdout 逐字节相同」从散文变成测量：`probe_run1_sha16=af63f4d21926660e
probe_run2_sha16=af63f4d21926660e … two_runs_stdout_identical=True`，两趟都 2530 字节、stderr 0 字节。
两条 pytest 也重取了 —— 因为 step 5 的载档没有为那三枚产品文件留 digest，「自 step 5 之后未动字节」
在我手上是不可测的一句话，重跑才是诚实的做法。

**顺序是这台仪器的一部分**（载档里写着为什么）：两条 pytest 排在 traceability **之后**。原因是本波撞见的
一个新事实 —— `tests/test_script_download_backend_iter2.py` 是一个**活写手**：它每次执行都往
`docs/evidence/C100/frontend-download/runs/<UTC 时间戳>_<8hex 运行 id>/` 落 `run-manifest.json` 与
`backend-exceptions-redacted.json` 两枚文件，而 traceability 走文件系统普查 `docs/evidence/`，
于是**静态面每跑一次，被静态面度量的那个母数自己涨 2**。读数：
`frontend_download_runs_dirs_total=55 dirs_born_after_step5=1 fd_charges_those_dirs=2`，
载档末尾那格 `frontend_download_dirs_after_pytest_born_after_step5=2
names=["20261018T002136Z_578b1bbd","20261018T002531Z_c2036978"]` 是同一代里当着面涨出来的第二枚 ——
前者是本 builder 上一代（pytest 排在前面那一代）留下的，后者就是本代这一格 pytest 留下的。
所以 step 5 → step 6 的 `+6`：命名写手解释 3（2j2 载档 1 + fd 收据 2），
`residual_unexplained=3` 仍然归因不了，根因是 step 5 只归档了总数没归档名单（§5 缺陷 10/11）。

**`make gate`（17 枚）仍未跑**，本载档最后一段自己写着这件事，不声称 gate 绿；157 那格 `PY_SELFDEV`
与本节这条 traceability 母数，仍是两个本波没动的 open mover。

## 8. 下一波谓词与复算配方

谓词（可反证）：plane 1 的 281/350 之后，残留 69 行里
`INCOMPLETE 8`（cboe 2 / econdb 2 / federal_reserve 1 / government_us 2 / intrinio 1，
全部 5 相 BLOCKED、`reason='no offline witness configured for this model row'`）需要的是
**分片与声明**，`UNREVIEWED 61` 需要的是**先跑 producer**。两者不能混成一个波。
**这 61 行不是零散缺失，而是整族的分片缺席。** 按 provider 现读（本轮普查，不是抽查）：
`yfinance 29 + tmx 24 + seeking_alpha 3 + wsj 3 + biztoc 1 + stockgrid 1 = 61`，相加正好等于
`UNREVIEWED` 那一格；而 CSV 的 32 家 provider 里 `docs/evidence/C101/<provider>/claims.json`
存在的是 **26** 枚，缺席的 6 枚恰好就是上面这 6 家（`shards_present=26 + shards_absent=6 = 32`，
两份名单由同一次 `os.path.exists` 循环一起打印）。`OFFLINE_ACCEPTED` 与 `INCOMPLETE` 两个桶
都没有这家族的人：`yfinance` 全部 29 行状态只有 `UNREVIEWED`，`tmx` 全部 24 行同理。
这五句的全部读数由 step 2j2 载档的 H/I 两臂携带（见 §6 末段），复算配方里就是同一条命令。
所以最大的一簇是 **yfinance 的 29 行**，tmx 的 24 行是第二簇 —— 本波第一稿把 tmx 写成"最大的一簇"
是把待办清单里见过的那个数当成了普查读数，已按 `shard_exists` 现读更正（见 §5 缺陷 7）。
本波的加宽只撤掉了 tmx 的**缝**这一根轴，step 0 对那 24 行的逐行读数仍是
`UNBOUND 16 + ROW_CLASS_NOT_CONTRACT 7 + NO_CODE_SITE 1`，且 24 行全部
`no_ledger=NO_SPECS_MODULE:ModuleNotFoundError`、`witness_module=False`。
所以把 tmx 算成"下一波跑一次 producer 就收"是错的：它先要注册绑定（那是冻结面，不是一枚分片）、
再要 specs 模块与 7 枚声明。谓词应写成
`tmx 的 16 行在 bound=False 转 True 之前不可能进入 ACCEPT`，
而不是"加宽还没做"。
失效条件：任何一次再改共享仪器（runner/producer/recorder）都作废 1405 条已执行 claim 的
`probe` pin ⇒ 必须再付一整轮 produce→sign→merge→judge。
失败读数：若某波 `accepted` 升而不变量探针 `moved>0`，说明这次重生产改到了判定而不只是 pin；
若 `rerun == archived` 为假而计数仍绿，说明载档里的 judge 报告与盘上 merge 不同代。

```bash
cd /Users/yunjinqi/Documents/new_projects/opendata && export PYTHONPATH=.
PY=/Users/yunjinqi/opt/anaconda3/envs/py313/bin/python
# 单家（deribit 本波用的形状）
$PY scripts/quality/iter2_claims_producer.py --provider deribit \
    --evidence-root docs/evidence/C101 --output docs/evidence/C101/deribit/claims.json
$PY scripts/quality/iter2_primary_review_recorder.py \
    --claims docs/evidence/C101/deribit/claims.json \
    --output docs/evidence/C101/deribit/claims.reviewed.json \
    --provider deribit --decision ACCEPT --reviewer-type primary_model \
    --reviewer-id iter2-acceptance-reviewer --reviewer-model qoder-cli-agent \
    --reviewer-role acceptance_reviewer
# 共享仪器改动后的整轮
$PY docs/evidence/C129/runner-seam-widening-20261018/probes/reproduce_all_shards.py
$PY docs/evidence/C129/runner-seam-widening-20261018/probes/reproduce_invariance_delta.py
$PY docs/evidence/C129/runner-seam-widening-20261018/probes/resign_all_shards.py
$PY scripts/quality/iter2_claims_merge.py --output docs/evidence/C101/claims.merged.json
$PY scripts/quality/iter2_model_evidence.py \
    --inventory "docs/迭代计划/迭代2-统一Provider架构与全量能力补齐/模型级任务清单.csv" \
    --claims docs/evidence/C101/claims.merged.json \
    --output docs/evidence/C101/judge/claims.merged.report.json
$PY docs/evidence/C129/runner-seam-widening-20261018/probes/verify_merge_judge_face.py
$PY docs/evidence/C129/runner-seam-widening-20261018/probes/post_format_two_generation_face.py
$PY docs/evidence/C128/plane1-residue-readiness-20261017/probes/landed_face_rebase.py
```

`post_format_two_generation_face.py` 只读载档与 git 对象，不写盘；它钉的波前是 full sha
`99834046c2db5eba1d39e9c8e176e0699a76a50c`（波 28 步骤 4 那次提交），所以本波提交之后它读的
还是同一个对象 —— 若那个对象消失，E 臂打印 `base report unreadable` 并转红，不会悄悄换基准。

step 6 那块静态面本身不住在仓库里（builder 在 `/tmp`，一次性），所以配方按**顺序**逐条给，
下一轮照抄顺序即可复算，尤其别把会写盘的 pytest 排在 traceability 之前：

```bash
$PY -m ruff check <写集 14 枚逐字路径>
$PY -m ruff format --check <同上>
$PY -m mypy opendata/data/providers/deribit/models/fetchers.py \
    scripts/quality/iter2_claims_probe_runner.py scripts/quality/iter2_witnesses/deribit.py
$PY -m ruff check --output-format=concise .        # 数 Found 行；按前缀分目录数 finding
$PY scripts/quality/evidence_traceability.py       # 先取母数
$PY docs/evidence/C129/runner-seam-widening-20261018/probes/post_format_two_generation_face.py  # 跑两趟比 sha
$PY -m pytest -q -o addopts= -p no:cacheprovider -k deribit
$PY -m pytest -q -o addopts= -p no:cacheprovider tests/test_congress_cboe_iter2.py \
    tests/test_script_download_backend_iter2.py    # 这格会往 C100 落 2 枚收据，故排最后
```

失效条件补一条（写给下一轮，也写给正在改 traceability 的人）：`docs/evidence/C100/frontend-download/runs/`
按**每次 pytest 执行**增长，所以任何"traceability 总数变了"的声称都必须同时给出
`frontend_download_runs_dirs_total` 与新增目录名；只报总数的那份载档（step 5）已经在这一点上归因不了，
不要重复这个形状。

## 9. 载体与摘要

配方里每一步单独取 exit；重签与 merge/judge 会写 `docs/evidence/C101/**`，
跑完 `git status` 里出现 review 时间戳与收据变化是预期，出现 `opendata/**` 变化就是探针自己写了盘。

| 载体 | sha16 |
| --- | --- |
| `probes/reproduce_all_shards.py` | `9c2d2fad7634738c` |
| `probes/reproduce_invariance_delta.py` | `3154f98be58f1d0d` |
| `probes/resign_all_shards.py` | `f0f3ed96c2734ec3`（那次 ruff format 之前是 `067fb9b23229cc7f`） |
| `probes/verify_merge_judge_face.py` | `b46d1e51fd260078` |
| `probes/post_format_two_generation_face.py` | `9e6df4a674bf18e3`（九臂定稿这代；七臂那代是 `4ef6812e677ffbc7`，两代都留字） |
| `runs/wave29-step2c-reproduce_all_shards.txt` | `6814f23a933a45da` |
| `runs/wave29-step2e-reproduce_invariance_delta.txt` | `19b8ffc6c9384771` |
| `runs/wave29-step2f-resign_all_shards.txt`（上一代） | `e1ff0e322fbdd109` |
| `runs/wave29-step2f2-resign_all_shards_post_format.txt`（当前字节） | `79161d862bafed6a` |
| `runs/wave29-step2g-merge_and_judge.txt`（上一代） | `e7d555fbce6fe22f` |
| `runs/wave29-step2g2-merge_and_judge_post_format.txt` | `4e1627bb3a8aed51` |
| `runs/wave29-step2h-verify_merge_judge_face.txt`（上一代） | `7dd745a8b416ff71` |
| `runs/wave29-step2h2-verify_merge_judge_face_post_format.txt` | `3dc894dbbebcce3f` |
| `runs/wave29-step2j-post_format_two_generation_face.txt`（七臂那代，A-G 块与 2j2 逐字相同） | `5873505140c4dc68` |
| `runs/wave29-step2j2-post_format_two_generation_face_9arms.txt`（定稿这代，加 H/I） | `6fdfdb621fb3edff` |
| `runs/wave29-step3-static_planes.txt`（argv 缺失那块，字面留着） | `50005a50ae09b97c` |
| `runs/wave29-step5-static_planes_retake.txt` | `a9919d9c32a026a6` |
| `runs/wave29-step6-static_planes_final.txt`（最终代，取代 step 5 的两格读数） | `5024bf7506d198ce` |
| `scripts/quality/iter2_claims_probe_runner.py` | `18b46adc3ccbcfb7`（HEAD 是 `6f73b11a14b8c81d`） |
| `opendata/data/providers/deribit/models/fetchers.py` | `c95af59ff225890f` |
| `scripts/quality/iter2_witnesses/deribit.py` | `af9d1913d5f9e73e` |
| `docs/evidence/C101/claims.merged.json` | `73c7701a6931d02c` |
| `docs/evidence/C101/judge/claims.merged.report.json` | `d61e03fb0a1c7ffb` |
| `docs/evidence/C128/.../runs/wave29-step2d-landed-face-rebase.txt` | `76c67441c9c8c41c` |

merge 与 report 这两格是本波最终代（step 2g2/2h2 之后）。step 2d 那份载档在 281 这一代上重跑过一次，
摘要仍是 `76c67441c9c8c41c`：两次运行相隔十几分钟、盘面 review 也换了代，产出却是同一串字节，
所以它打印的面里没有随代次变化的格（这与 merge/report 两格形成对照，那两格必然会变）。

landed 常数重定基（step 2d，`landed_face_rebase.py` 12 臂 / 12 过）落在 C128 的两台仪器里：
`ledger_axis_blast.LANDED_STATUSES = {INCOMPLETE: 8, OFFLINE_ACCEPTED: 281, UNREVIEWED: 61}`、
`witness_seam_kind_census.LANDED_ACCEPTED = 281`，两者的"波前"常数分别留作伪造反例。
