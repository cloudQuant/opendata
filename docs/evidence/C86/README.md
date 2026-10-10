# C86 —— 七个原本无探针的 gap 格子改由判定探针实测，AC-17|02 的例外普查改成与 bandit 同语义，AC-5|07 的过期 bundle 重扫

本轮三件事：把登记表里 7 个「无人测量的 gap」接上真判定（`scripts/quality/acceptance_item_probe.py` 新增 7 员探针），
修掉一处仪器缺陷——AC-17|02 的 `# nosec` 普查按整行 grep 计数，而 bandit 按注释令牌计数，两者对不上时
探针会声称一个扫描器从未读过的例外；以及清掉本轮门禁停在 member 11 的 stale-proof 红格 AC-5|07（bundle 过期，
重扫而非刷时间戳）。顺带把「本目录的例外到底压住了什么」做成一台逐文件对 bandit 自身记账的仪器，因为它在自己的
普查根之外。三处都不改判据方向：新探针一律先落在 gap，例外普查是**增加**一条检查（切分失败的文件必须为 0），
bundle 的 24h 新鲜度一条也没放宽。

## 目录内容与各自的来源

| 文件 | 是什么 | 生成命令 |
| --- | --- | --- |
| `probe-seven-carrier.py` | 载体生成器：跑 shipped 探针，stdout 逐字节粘进档案，头部记 HEAD/解释器/探针模块摘要/`git status --porcelain` | `python docs/evidence/C86/probe-seven-carrier.py` |
| `probe-seven.txt` | 七格首次判定读数（每格 `VERDICT` 与逐面列表） | 同上（默认七格） |
| `probe-seven-readings.json` | 同一批读数的结构化副本，`ledger-seven-patch.py` 的 `reason` 逐字取自这里 | 同上（`--json`） |
| `ac17-02-nosec-census.txt` / `ac17-02-readings.json` | AC-17\|02 修复后的实测读数（`proven`，15 条反例臂全咬） | `python docs/evidence/C86/probe-seven-carrier.py --item 'AC-17|02' --out docs/evidence/C86/ac17-02-nosec-census.txt --json docs/evidence/C86/ac17-02-readings.json --title '…'` |
| `nosec-census-compare.py` / `.txt` | 退役 grep 普查与 shipped 注释令牌普查并排对照，两侧都打印 | `python docs/evidence/C86/nosec-census-compare.py` |
| `roster-census-coverage.py` / `.txt` | C64 仓表盘点的 census 覆盖面：差额是载体截断还是正则漏读 | `python docs/evidence/C86/roster-census-coverage.py --self-check` |
| `probe-arm-census.py` / `.txt` | 探针模块自己的反例臂普查：逐格臂数、与 `--self-test` 总数的对钉、与 HEAD 的闭合算术 | `python docs/evidence/C86/probe-arm-census.py --against 00dd736 --expect-arms 939` |
| `repin-label-check.txt` | 重钉条款的署名普查：实拼 0 处相邻重复词、forged 形式恰 1 处并点名 | `python docs/evidence/C85/reference-policy-repin.py --label-check`（对照臂加 `--label-check-tamper`） |
| `repin-write-capture.txt` | 审计 + `--write` 回执逐字存档（摘要 `c4521187… -> 521d2309…`、`REAL problems: 0`、checker exit 0） | `python docs/evidence/C85/reference-policy-repin.py` 然后 `… --write` |
| `repin-controls.txt` | 重钉仪器的三条对照臂（翻 hex / 偷渡 import / 品牌名注释）与巡回后清单字节未变面 | `python docs/evidence/C85/reference-policy-repin.py --tamper-policy`（另两条同形） |
| `ledger-seven-patch.py` | 把七格的 `state/round/date/command/reason/evidence/note` 改写为探针读数 | `python docs/evidence/C86/ledger-seven-patch.py` |
| `gate-run1.txt` | 第 1 遍全门禁日志：member 1—4 绿、member 5 红在本日志自身的 2 条面上 | `make gate`（第 1 遍日志写在仓内，第 2 遍改写到仓外再入档） |
| `gate-run2.txt` | 第 2 遍全门禁日志（`/tmp` 采集后逐字节入档）：member 1—10 绿、member 11 红在既有 stale-proof 两格，`反事实面 130/130、939 条 break` 逐字在档 | `make gate`（日志 `> /tmp/c86_gate_run2.log`，跑完 `cp` 入档） |
| `gate-run3.txt` | 第 3 遍全门禁日志（HEAD `a2443e6`，**干净工作树起跑**）：member 1—10 绿（`A2 files: 750`）、member 11 的 `deferred` 归 0、`stale-proof` 只剩 `AC-11\|04` 一格，`GATE_EXIT=2`；门禁 fail-closed，member 12—17 本轮没有面 | `make gate`（日志 `> /tmp/c86_gate_run3.log`，跑完逐字节入档；墙钟与退出面取自包装器自己的输出） |
| `gap-reason-staleness.py` | gap 格子的台账 `reason` 与探针现读数对账：A state、B reason×`round` 标签交叉、C 借用键对、D 数字残差披露（带 `cited` 分母）、E 两处被现读数改写的阻塞原因逐字并排，另加两条控制臂 | `python docs/evidence/C86/gap-reason-staleness.py` |
| `staleness-carrier.py` | 上一员的载体生成器：stdout 逐字节入档，头部钉 HEAD/解释器/**仪器与探针两份**摘要 | `python docs/evidence/C86/staleness-carrier.py` |
| `gap-reason-staleness.txt` | 上面那台普查的 **live 载体**：16:04:50、HEAD `b190b5f`、仪器 `301061ae…`，14587 字节，含 F 面（按仓库自己的运行面校验器复算 issue 类别） | `python docs/evidence/C86/staleness-carrier.py`（内跑 `gap-reason-staleness.py`，头部记 inner command） |
| `gap-reason-staleness-pass2.txt` | **同一普查在归档门禁第 2 遍时刻的读数**（15:26:39、HEAD `c7bad6e`、仪器 `b35de44a…`，12815 字节；文件名里的「第 2 遍」指门禁遍次，不指时间先后——它比上面那份早 38 分钟，两份都留档是因为 F 面在那之后才加） | 同上 |
| `ac1-05-manifest-face.py` / `.txt` | AC-1\|05 的 manifest 面：686 条目 = 232 clean + 454 flagged 的记账恒等式、461 条 issue 的分类、两个缺失顶层目录今天是否还存在，并配 present/absent 两条方向臂 | `python docs/evidence/C86/ac1-05-manifest-face.py` |
| `ac5-07-bundle-freshness.py` / `.txt` | AC-5\|07 的安全 bundle：本轮真重建（`--force`）+ 走 validator 自己的 `now` 缝做三条时钟臂，证明新鲜度判据仍然咬人 | `python docs/evidence/C86/ac5-07-bundle-freshness.py`（副作用：重写 bundle） |
| `ac5-07-probe-rerun.txt` / `.json` | 重建之后 AC-5\|07 的定向探针读数（`proven`，`issue=0`） | `python docs/evidence/C86/probe-seven-carrier.py --item 'AC-5|07' --out docs/evidence/C86/ac5-07-probe-rerun.txt --json docs/evidence/C86/ac5-07-probe-rerun.json --title '…'` |
| `ac17-02-nosec-census-rerun.txt` / `.json` | 新增仪器后 AC-17\|02 的重读：逐行 nosec 仍 157 处（普查根不含 `docs/evidence`，见下节的范围面） | 同上（`--item 'AC-17|02'`） |
| `nosec-audit.py` / `.txt` | 本目录 28 处例外**逐文件**对 bandit 自己的 `skipped_tests` 记账，另加 `[0]` 范围面（普查根从 `Makefile` 实测读出、A2 文件集从 `resolve_files(None)` 实测数得）与三条反例臂（多点名 / 少点名 / 将本轮真实的那处过度点名放回） | `python docs/evidence/C86/nosec-audit.py`（临时副本写 `docs/evidence/C86/.scratch_nosec/`，出判定前删除并实测 `scratch_cleared=True`） |
| `ac9-08-arm-check.py` / `.txt` | AC-9\|08 这一格的**文案面**复校：3 个 census 标记各自命中的行号与数字、`repair` 后读得到 `proven`、声明的 8 条反例臂逐条把它打回 `gap`、2 条 null 臂（篡改没有臂认领的 `passthrough_rows`/`reader_rows`）读数不动、最后量「本轮驳掉的旧文案」在 shipped 源里还剩几处 | `python docs/evidence/C86/ac9-08-arm-check.py` |
| `ac9-08-reason-rerun.txt` / `.json` | 文案改完之后的单格探针读数（`PROBE_EXIT=0`，`VERDICT AC-9\|08: gap — …`），台账那一格的 `reason` 逐字取自这一行 | `python docs/evidence/C86/probe-seven-carrier.py --item 'AC-9\|08' --out … --json … --title '…'` |
| `ledger-reason-refresh.py` / `.txt` | 台账 `reason` 重发布：文案取载体自己的 `VERDICT` 行，`round`/`date` 从载体目录名与载体的采集时间行实测（不在仪器里手写轮次），写前 RED / 写入 / 写后 GREEN 三遍逐字入档；`--write` 末尾把刚写的文件**重新解析**一遍，要求 reason、round 与每条 `evidence` 路径都 repo-relative 且在盘上 | `python docs/evidence/C86/ledger-reason-refresh.py --carrier docs/evidence/C86/ac9-08-reason-rerun.txt [--verify-only\|--write]`（三遍） |
| `ledger-reason-refresh-controls.py` / `.txt` | 上面那台仪器的五条控制臂 + 回滚面：台账没有的格子要 rc=1、载体 `proven` 而台账 `gap` 要 DECLINED 且字节不动、指向一份**没有 `VERDICT` 行的真档案**要 rc=1、对已写台账 `--verify-only` 要 rc=0、同一条读数 dry 不动 sha 而 write 会动，控制臂自己那次写回滚后与保存字节逐字节相等；头部与 tally 都钉住被测仪器的摘要 | `python docs/evidence/C86/ledger-reason-refresh-controls.py` |

## AC-17|02：例外普查的语义要与扫描器一致

bandit 的 `core/manager.py` 只从 `tokenize.COMMENT` 令牌里读 `# nosec`，并且一个文件切分失败（`TokenError`）时
该文件的 `nosec_lines` 为空——**一条例外都不认**。旧普查按整行 grep，于是把写在字节字面量里的 `# nosec` 也算成例外。
`nosec-census-compare.txt` 把两侧并排打印（对照臂是本地重建的旧形状，不是再导入一次新代码）：

- `grep_census_sites=162`、`comment_census_sites=157`，差 5 处；逐条点名 5 处 `GREP-ONLY`：
  `scripts/quality/write_benchmark_evidence.py` 的 4 处字节字面量，以及 **`acceptance_item_probe.py:13668`——修复自身
  的 docstring 里那句「bandit keys ``# nosec`` …」**。这一条是旧普查的活性危害证明：一个只解释例外的散文行会被算成
  一处「缺规则号的例外」。
- 装饰性 nosec（什么都没压住）grep 侧 4 处、注释令牌侧 0 处；缺规则号 1/0；缺理由 2/0。
- 双侧都真的跑过 bandit：`remaining=0 raw=161 skipped_self_report=161`，两臂差额与自报压住数相等。

新面 `nosec_unreadable_files` 是**加**上去的判据（`== "0"`），并配一条反例臂（`*nosec_unreadable_files+1` → gap），
所以那个 0 有可返回非 0 的臂；修复后 AC-17|02 仍为 `proven`，15 条臂全咬。

## C64 盘点的两个差额：是载体的，不是普查的

AC-8|08 从 `docs/evidence/C64/warehouse-readonly-inventory.txt` 读出 `roster_table_delta=3`、`roster_rows_delta=101`
（表头声明 `tables 12 estimated rows 12461018`，正文 9 行合计 12460917）。这两个数有两种成因：载体只落了 9/12 行，
或者判定正则认不出其中 3 行。`roster-census-coverage.py` 用探针模块自己的 `C86_WAREHOUSE_TABLE` 数成对的两条：

```
[live] body_tuple_lines=9 census_matched=9 unmatched=0
[live] declared_tables=12 matched_names=9 distinct_names=9 table_delta=3
[live] declared_rows=12461018 listed_rows=12460917 rows_delta=101
CONTROL unmatched clean=0 tampered=1 -> PASS
```

`unmatched=0` 与 `table_delta=3` 同时成立 ⇒ 差额全部来自载体自身：C64 档案是外部日志
`opendata-iter01-existing-warehouse-readonly-20260930.log` 的摘录，该日志不在仓内，缺的 3 张表在档案里连一行都没有。
对照组把一条名字带连字符的表行接到载体尾部，`unmatched` 立刻变 1 并逐字打印该行——若是普查漏读，签名是
`unmatched=3 且 table_delta=3`，与实读不同。该缺陷方向的歧义只会造成假红（重跑盘点后 `table_delta` 仍为 3 就判不出
`proven`），不会造成假绿。

## 七格首次被测量

七格此前在登记表里是 gap，但没有任何探针测过它们——gap 只是一个手填标签。现在每格的 `reason` 都由探针逐面打印，
`ledger-seven-patch.py` 逐字抄入，并且写入后可复验：

```
$ python docs/evidence/C86/ledger-seven-patch.py --verify-only
VERIFY cells=7/7 problems=0
```

这条读回判据在改 AC-8|08 的 note 时先红过一次，可见它不是恒真式：
`PROBLEM AC-8|08: note does not equal this script's NOTES entry` → `VERIFY cells=7/7 problems=1`（rc=1），
`--refresh-notes` 之后才转 0。它逐格要求四件事同时成立：`reason` 与 `probe-seven-readings.json` 逐字相等、
`state=gap` 且 `round=C86`、`note` 等于脚本自身的 NOTES 条目、每一条 `evidence` 路径在盘上存在且含载体。
其余护栏：任一探针读数不是 gap 就中止（那意味着格子该翻面而不是刷新），已带 `round=C86` 的格子再跑一次会中止
（幂等，实测 `REPEAT_RC=1`），改写格数不等于 7 则中止。`python scripts/quality/acceptance_ledger_check.py` 复跑
`items=130 proven=112 gap=18 unreviewed=0 ticked=112`。

七格仍全部记为 gap，未做的都是需要授权的动作：逐腿人工签署与冲突样本修数据（§4|02）、D10 复权链未判读腿（§4|04）、
本轮供应商取数与当前全市场范围自证（§5|02）、仓表盘点/两源行数一致面（AC-8|08）、交易日内 17:30 注册窗口的真机实测
（AC-13|08）、旧库账户改只读（AC-15|01）、业务映射与迁移/DROP 动作（AC-15|02）。

## 臂数不靠手抄：普查与 `--self-test` 对钉，并与 HEAD 做闭合

本轮笔记要写「7 员新探针、多少条反例臂、AC-17|02 有几条」。`--self-test` 只打印一个总数，逐格数字若由人从散文里
抄出来就是下一个会腐烂的数，所以 `probe-arm-census.py` 走的是自测同一批对象（`PROBES` 与每员声明的 `Break` 列表），
并把自己钉到自测打印的那个数上：

```
probes=130 declared_arms=939 min_arms_per_probe=2
probes_without_arms=0
against=00dd736 head_probes=123 head_arms=859
delta probes=+7 arms=+80
new_item_arms=79 existing_item_arms={'AC-17|02': 1}
closure 79+1=80 vs 939-859=80 -> HOLDS
TIE total=939 self_test=939 -> MATCH
```

`probes_without_arms=0` 的分母论证是同行的分布面 `min_arms_per_probe=2`：0 不是「没数到」，而是最小格也有 2 条臂。
七格逐格臂数 12/13/16/10/6/9/13（§4|02、§4|04、§5|02、AC-8|08、AC-13|08、AC-15|01、AC-15|02），合计 79；
HEAD 快照在临时树里复算（`git show` 进 `docs/evidence/C86/.scratch_head/`，跑完即删），123 员 859 条，
唯一变动的旧格是 AC-17|02（+1，即新增的 `nosec_unreadable_files` 臂）。闭合式 `79+1 = 939-859` 不成立时脚本返回 1。

对照臂：`--expect-arms 938` → `TIE total=939 self_test=938 -> MISMATCH`、`CONTROL_RC=1`。
这里也要留一个我自己错过的数：本轮早先一次自测读的是 **938**（在 AC-17|02 那条新臂落盘之前），它已经没有仓内载体，
只在这段散文里留名；shipped 的读数是 939，且 938 与 939 的差被上面的闭合式逐成分解释了。

## 摘要重钉：批准链条款的署名也要实测

`scripts/quality/acceptance_item_probe.py` 是批准清单（`docs/quality/akshare-reference-allowlist.json`，87 条）里
被钉摘要的一条，本轮改动必然移动它。重钉走 `docs/evidence/C85/reference-policy-repin.py`（回执见
`repin-write-capture.txt`）：`c4521187… -> 521d2309…`、`entries re-pinned: 1`、`REAL problems: 0`、
`checker exit code: 0`，扫描器 `(module, kind)` 命中多重集 pinned=13 live=13、策略过滤后仍 13->13、加载形态行 4==4，
所以这是同一批准内容下的摘要重钉，不是新的批准。三条对照臂在 `repin-controls.txt`：翻一位 hex → checker exit 1
且点名恰那一个路径、还原后 exit 0；偷渡 `import akshare` → problems 3（多重集、`load_shape_lines`、加载形态行各一条）；
只写一条品牌名注释 → problems 0 但把 `added brand lines: 1 / of which load shapes: 0` 分类打印出来。
控制巡回前后清单字节 `相等=True`。

这里也有一条本轮自己的错判入档：条款由 `signer(mover)`（读 mover 提交标题里的 `C\d+`）与标记常量拼接，
而标记常量原来收进了 `primary` 一词，于是第一次 `--write` 落进批准链的是
`C86 primary primary 复核 sha 漂移…`（重复词）。该仪器不在 87 条注册项里、也不在扫描器 walks 的树内，
所以这次编辑不移动任何被钉摘要；处理方式是把带错条款的清单 `git restore` 掉、把标记收成
`复核 sha 漂移`、整批重跑（`--write` 与三条对照臂都重跑了一次），并把这一失效形状做成可判面：
`--label-check` 打印实拼条款的相邻重复词数（live=0）与一个刻意重复的 forged 条款（control=1 且点名
`primary`），两侧都必须成立才 PASS（`repin-label-check.txt`，正臂 rc=0、`--label-check-tamper` 臂 rc=1）。
同一条款此前还有一个更隐蔽的错：`signer` 之前是字面量 `C85`，于是 C86 的 `fb904f2` 移动字节后，
批准链里出现一条署名 C85 的重钉条款——轮次标签没有测量支撑，现在改为从提交标题取。

## AC-5|07 的红是 bundle 过期：修法是重扫，不是改时间戳

`docs/quality/acceptance-criteria.md:165` 要求「搬运层安全扫描（bandit 含 B 层）完成并人工 triage 留档」，
而 `scripts/quality/ported_security_evidence.py` 对 bundle 有 `MAX_EVIDENCE_AGE = 24h`。C74 那两份的
`generated_at` 停在 `2026-10-09T03:47:11+00:00`，本轮跑到 10-10 就过期，于是探针读出 gap、台账却记 proven
（`stale-proof` 红格）。两条诱惑都不许走：把 `generated_at` 改新是伪造新鲜，放宽 `MAX_EVIDENCE_AGE` 是改判据。
走的是仓库自己的构建器真重扫：`ported_security_evidence_build.py --force` 对 `opendata/data/providers/akshare/_vendor`
的 325 个 py 文件重跑 bandit 1.9.4。

```
[1] builder … --force rc=0 built=True   finding_count=1048 high_count=31 identities_matched=1048
    python_files=325 rule_count=15 scanner_exit=1 carried_from_round=C65 source_sha256=0499005a…
[3] recorded generated_at=2026-10-10 08:35:53+00:00 -- three clock readings of the same bytes
    live(now=None)                          valid=True  issues=0
    aged(now=generated+1 day, 1:00:00)      valid=False codes=scan-stale,triage-stale
    future-dated(now=generated-1:00:00)     valid=False codes=scan-generated-at-invalid,triage-generated-at-invalid
[5] arms live_clear=True aged_names_stale=True future_dated_names_invalid=True arms=3 -> PASS
```

- 重建的证据强度落在 diff 上：`git diff --numstat` = `1/1` 与 `2/2`，`-U0` 展开只有三行
  `+`/`-`——两份的 `generated_at`（`03:47:11 → 08:35:53`）与 triage 里那条 `scan_sha256`
  钉（`90947021… → 456cddf9…`）。**findings 本体逐字节未变**，所以这是「同内容重扫 + 新时钟」而不是给旧档案刷时间；
  `source_sha256` 与上一轮相同（`0499005a…`）说明搬运树确实没动，`identities_matched=1048` 说明逐条结转 C65 处置
  靠的是 finding identity 集合相等。
- 三条时钟臂都走 validator 暴露的 `now` 缝，没有为了「回到未来」编辑任何文件。中间那条臂是必须的：一个只有
  `issues=0` 的读数是空判，新鲜度规则可能早已是装饰；`aged` 臂必须真报 `*-stale`、`future-dated` 臂必须真报
  `*-generated-at-invalid`，两侧都不咬才算这台仪器没测到东西。
- 重扫之后定向探针重读 `VERDICT AC-5|07: proven`（`census: proven=1`），面里逐字带
  `issue=0`、`记录 1048 项 / 241 个有 finding 的文件 / 15 条 B 规则`。顺带清掉一个歧义：`scan_files=241`
  不是覆盖率缺口，validator 数的是「带 ≥1 finding 的文件」，325 才是扫描文件数，两者不同名。
- 一条自己的仪器缺陷入档：首跑在 `[3]` 面中途抛 `AttributeError: 'ValidationResult' object has no attribute 'state'`
  ——`state` 是我猜的，该类只暴露 `facts/issues/valid`。崩在面中途的代价不只是半截 stdout：**档案是一份都没写出**
  （写档在 `main()` 末尾），所以「仪器跑过」与「载体存在」是两件事，属性名要先读 dataclass 再用。

## 本目录的 28 处例外逐文件对 bandit 自己的 `skipped_tests` 记账

先量范围，因为它决定了这一台为什么必须存在：AC-17|02 的例外普查根来自 Makefile 的 `PY_BANDIT`
（`opendata, scripts, alembic, alembic_data`），**不含 `docs/evidence`**；而 A2 的文件集
（`resolve_files(None)` = 750 个，其中 106 个在 `docs/evidence/**`）**含**。两侧都不是装饰性例外的判据：
目录内「该点而没点」的违例会被 member 8 打红，但「点了却没东西可压」在这一带打不红任何东西，AC-17|02 也看不见——
本轮新增仪器带来的例外改动之后重读 AC-17|02，逐行 nosec 仍 **157 处**、反向臂仍 161 条，就是这个范围面的实证。

这一段早先的两个数（普查根那四个、A2 的 `750 / 106`）是拿一次性只读命令量出来写进散文的，也就是说它们当时是
**没有载体的数**。现在两个测量进了仪器自己：`[0]` 面把普查根从 `Makefile` 里那行 `PY_BANDIT :=` 实测解析出来
（读不到该行就中止并打印 FAIL，而不是默认一组根），把 A2 的文件集从 `scripts/quality/a2_check.py` 的
`resolve_files(None)` 实测数出来，并把两条都做成**方向可翻**的面——把 `docs/evidence` 并进去时范围谓词必须变
True，同一个计数器对着一个不存在的目录必须返回 0：

```
[0] scope: Makefile line `PY_BANDIT := opendata scripts alembic alembic_data` -> census roots ['opendata', 'scripts', 'alembic', 'alembic_data']
    is docs/evidence a census root = False; widening to [...] would read True, which is the direction that makes this face bite
    the gate's A2 member (resolve_files(None) of scripts/quality/a2_check.py) walks 750 files, 106 of them under docs/evidence; the same counter on docs/evidence/C99_no_such_round -> 0
    so an unexempted violation in these instruments fails member 8, while a decorative exemption fails no gate member at all -- that middle is what this file judges
```

`scope_ok` 因此进了末条合取式，不再只是一段说明。

判据形式取逐文件：`surplus = 声明站点数 - bandit 自己的 metrics._totals.skipped_tests`。bandit 1.9.4 在 `-q`
下对装饰性 nosec 不打印任何东西（连 `nosec encountered, but no failed test` 都没有），所以警告字符串不是可量的面。

```
[1] files=11 (enumerated, not typed) files_with_exemptions=8 exemption_sites=28
[2] declared={'B404': 8, 'B603': 15, 'B607': 5} surviving_after_nosec={}
[3] pooled per-id presence census: decorative=[] undeemed=[]
[3c] parts tie to the whole: sum(per-file skipped)=28 pool=28 | sum(per-file sites)=28 pool=28 | sum(surplus)=0
[3d] required disagreement docs/evidence/C29/real-tree-probe.py: line_dead=3 surplus=0 skipped=4
[3e] fired=28 skipped=28 surviving=0 -> additivity=True; declared_sites=28 surplus=0
[6] clean=True scope=True … -> NOSEC_AUDIT_CHECK PASS
```

这台仪器抓到两处真的过度点名，都不是靠人眼：

1. `probe-seven-carrier.py:33` 带 `# nosec B603 B607`，而那个调用的 argv 是元组、没有字面量路径，
   bandit 在此只 fire `B404@18 / B603@33 / B603@65` ⇒ B607 从未被消费（`surplus=1`）。处理是把 B607 从**载体**上删掉。
2. 本仪器自己：`nosec-audit.py:72` 的**散文注释**里写了「`# nosec B603`」。注释令牌正是 bandit 读例外的地方，
   于是一行解释例外的话自身成了装饰性例外，`surplus=1`、`dead_ids={'B603': 1}`，门禁因此拒绝 PASS。
   改写这段散文后站点数 29→28、surplus 归 0——与 AC-17|02 那条 `acceptance_item_probe.py:13668`
   （修复自身的 docstring 被旧普查数成例外）同族，本轮复发在自己身上。

三个被推翻的判定形状都留在档里，而不是下次重猜：

- 头部 `git rev-parse --abbrev-ref HEAD --short HEAD` 一次 argv 只把分支名打印两遍、一个提交都不给，
  也就是说溯源面本身就是 bug；现在是两次调用并分别打印。
- 复刻的 `# nosec` 正则 `B[\d.\s,]+` 会在第二个 id 的 `B` 处停住，于是 `# nosec B603 B607` 被读成一个 id，
  而 bandit 真 fire 的 B607 被报成 `undeemed`——一条对我自己工具的假指控。改用 shipped 的 `NOSEC_PAT`
  取整段、`RULE_ID` 取号，并把退役形状在 `[4]` 旁并排打印（`shipped=[['B603','B607']]` vs `retired=[['B603']]`）。
- **按行键**的普查：bandit 的抑制覆盖整条语句，`# nosec` 写在多行调用的起始行是真例外，而
  `results[].line_number` 指向语句内部更靠后的行。按行键我曾把 14 处老档案的例外判成死例外，扫描器其实全消费了。
  因此 `docs/evidence/C29/real-tree-probe.py` 进总体做成「必须不一致」面：注释在 24/51/79/130、findings 在
  24/56/84/135、`skipped_tests=4`、`surviving=[]` ⇒ `line_dead=3` 而 `surplus=0`。哪天这两个数相等，
  换帧就没被证明，`line_frame_over_reports` 转红。按 id 汇算是**跨文件看不见**，按行键是**同文件假阳**，
  方向相反的两种错，门禁取逐文件。

三条反例臂都必须能翻面：`CONTROL-DECORATIVE` 给干净文件加一条永不 fire 的 B608 ⇒ `surplus 0→1` 且
`dead_ids={'B608': 1}`、无新违例；`CONTROL-UNDEEMED` 从双 id 那一行删掉一个 id ⇒ 该违例带红 rc 回来
（`surviving={'B607': 1}`、`skipped 4→3`、`rc=1`），这是「例外是承重的」而非装饰的证明；
`CONTROL-BLIND` 把本轮真实那处过度点名放回临时副本 ⇒ 逐文件帧点名（`surplus=1`、`dead_ids={'B607': 1}`）而
**汇算帧仍然沉默**（`decorative=[]`，`B607: declared=6 fired=5`）。第三条臂的期望值就是「汇算侧看不见」，
它证明的是这台仪器存在的理由本身，而不是某个格子干净。

副作用也实测而不是声称：临时副本写在 `docs/evidence/C86/.scratch_nosec/`，跑前先 `rmtree`（上一轮崩在这里的话，
伪造的违例会被 member 5 记成 untracked、被仓内普查计数），出判定前再删并打印 `scratch_cleared=True`。

## 门禁

第 1 遍（`gate-run1.txt`，HEAD `30cd9cf`，跑前 porcelain 只有 1 行且就是这一份日志本身）按 Makefile 顺序跑到
member 5 停：1 brand、2 zero-dep、3 secret、4 ledger 全绿并逐面留字——零依赖自测 `13 violations detected,
4 compliant samples clean`、扫描面 649 文件（opendata=322 / _vendor=325 / opendata_client=2）、gitleaks
`331 commits scanned / 150.68 MB / no leaks found`、登记表 `items=130 proven=112 gap=18 unreviewed=0 ticked=112`
（22 组 + 19 条 §10 行）。member 5 报出 2 条新的 traceability violation，两条都指向**正在被写入的那份日志自己**：

```
    exit: docs/evidence/C86/gate-run1.txt
    untracked: docs/evidence/C86/gate-run1.txt
```

这两条性质不同，且都做了两侧实测而不是推断。`#exit` 是自指读数：member 5 读文件的时刻，`GATE_EXIT=` 行还不存在，
跑完之后再对同一份字节复跑判据读得 `exit gaps = 0`（上面打印的 census 行 `dates in gate logs = header 125/134`、
`exit gaps = 0` 即复跑结果），所以这条红不是产品的退出面缺失，而是「门禁日志写进被门禁普查的目录」这一做法的产物。
`#untracked` 则是真实的记账顺序，必须靠提交清除。据此第 2 遍改为把日志写到仓外（`/tmp`），跑完再入档提交，
与 `docs/evidence/C76/gate-run5-traceability-red.txt` 记下的同一缺陷同做法。

还修了一处判据的「意外通过」：入档前查了是哪一行让 `command` 面变绿，用 shipped 的 `COMMAND` 正则逐行量得
`# python: Python 3.11.8 @ …`（解释器行）单独就返回 True，也就是说这一面是被一行**没有点名任何命令**的字节
偶然满足的；因此头部补了一行 `# command: make gate`，让命令面由它声称的那条命令来承载。

第 2 遍（`gate-run2.txt`，HEAD `f14a616`，日志写到 `/tmp/c86_gate_run2.log` 跑完再逐字节入档，
`47533` 字节、`sha256[:16]=b67391fe7941c8d9`，入档前后字节 `相等=True`）过了 member 5：

```
  narrative  gaps = 0
  date       gaps = 0
  identity   gaps = 0
  command    gaps = 0
  exit       gaps = 0
  untracked  gaps = 0

OK: evidence archive matches the frozen baseline (0 legacy entr(ies)).
```

member 6—10 同轮转绿（member 8 `A2 files: 745` 且 `OK: A2 files meet the full A2 standard (ruff + format +
mypy + bandit)`，member 10 `docstring coverage 100.0% (1397/1397)`）。这一遍停在 member 11，
`GATE_EXIT=2`，红因是既有的 stale-proof 两格而不是本轮新档案：

```
  - 反事实面：130/130 个探针走到了判定，939 条 break 各被施加一次、每条都要求把干净读数打回 gap
  - 台账↔读数：agrees=126, unflipped=0, open=0, deferred=2, stale-proof=2（共 130 格有读数） —— 只有 stale-proof
    是红灯：台账记 proven 而探针现在读出 gap，且读的那一面与这一刻无关；红格子 AC-11|04、AC-5|07
  - deferred（时刻面，等工作树干净再判）：AC-17|10、AC-1|10
```

`反事实面：… 939 条 break` 正是臂普查那一面要用来自钉的分母：`probe-arm-census.txt` 独立数得
`declared_arms=939` 并与 `--self-test` 的总数 `MATCH`，两把尺子（一个逐格 import 普查、一个门禁内部施加）
在同一轮各自读出 939，`applied` 没有小于声明数，所以本轮不存在「臂声明了却没施加」的仪器缺口。

`deferred=2` 这一条是本轮自己造出来的：member 11 跑到的时刻，工作树里有 3 项未提交
（日志同轮打印 `工作树：3 entry(ies), first:  M docs/evidence/C86/README.md —— worktree/index/history 面只在
干净时判，否则记为 deferred 并点名`），于是 AC-17|10 与 AC-1|10 这两员读 moment 面的探针被推迟而不是判定。
它与「日志写到仓外」是同一类教训的两面：前者防的是 member 5 把正在写的日志算成自己的 violation，
后者防的是**任何**未提交编辑让时刻面当轮不判——所以入档顺序必须是「先提交档案，再跑门禁」，
而不是边跑边改仓内文件。

第 3 遍（`gate-run3.txt`，HEAD `a2443e6`，`17:22:53` 起、`17:38:15` 止，46915 字节、`sha256[:16]=7534decc3022fd5a`、
`wc -l` 1011 行 = 408 字节头部 + 仓外日志的逐字主体）就是按上面那条结论改的入档顺序：**先把档案全部提交，再跑门禁，
跑期间不动仓内文件**，日志落 `/tmp/c86_gate_run3.log` 跑完再逐字节入档。头部因此 `git status --porcelain` 为 0 行，
member 11 同轮读到 `工作树：干净`：

```
  - 本遍墙钟 = 756.7 s，读到事实的探针 130/130，测不出来的：无
  - 工作树：干净 —— worktree/index/history 面只在干净时判，否则记为 deferred 并点名
  - 反事实面：130/130 个探针走到了判定，939 条 break 各被施加一次、每条都要求把干净读数打回 gap
  - 台账↔读数：agrees=129, unflipped=0, open=0, deferred=0, stale-proof=1（共 130 格有读数）
    —— 只有 stale-proof 是红灯：台账记 proven 而探针现在读出 gap，且读的那一面与这一刻无关；红格子 AC-11|04
  - 面基线：130 个探针里 25 个在读 moment 面；proven 而无探针 0 个，基线 0 个（只降不升）
```

member 1—10 同轮全绿，逐面在档：零依赖自测 `13 violations detected, 4 compliant samples clean`、扫描面 649 文件、
gitleaks `335 commits scanned / no leaks found`（第 2 遍读的是 332，差的 3 笔正是 `f14a616..a2443e6` 那三笔提交，
`git rev-list --count` 两侧同值可复核）、登记表仍
`items=130 proven=112 gap=18 unreviewed=0 ticked=112`、member 5 六面 `gaps = 0` 且 `files census = 1153 (gate logs: 135)`、
member 8 `A2 files: 750`（745→750 是本轮五份新仪器，`removed 0`）、member 9 `quality debt did not increase`、
member 10 `docstring coverage 100.0% (1397/1397)`。

`deferred` 从 2 归 0、`agrees` 从 126 到 129，涨的 3 格是**两件事**而不是三件事，且这个算术两侧都有面：
AC-17|10 与 AC-1|10 因工作树干净被真正判定且一致（+2），AC-5|07 因本轮真重扫 bundle 而翻回 `proven`
（同一份日志逐字有 `VERDICT AC-5|07: proven`，+1）；`stale-proof` 相应地从 2 降到 1。

剩下的那一格本轮**不离线修**，因为它的红不是文案：AC-11|04 的判据要求留档 EXPLAIN 正文里的 `query_module_sha`
与当下 `opendata/pipeline/query.py` 的字节摘要相等，等式要成立只能重取一次 EXPLAIN，而那是要连仓的动作。
门禁把这一格的形状自己说了一遍：`台账把 AC-11|04 记成 proven，探针现在读出 gap（被读的面：文件内容，与这一刻无关）
—— 台账翻上去之后没有任何东西再量它，这一遍就是那个东西`。这一格正是判据设计要抓的那个形状，
不是要绕过去的那一个。

门禁 fail-closed，所以第 3 遍的边界要写清：member 11 红 ⇒ `make[1]: *** [acceptance-probe-check] Error 1`、
`make: *** [gate] Error 2`、`GATE_EXIT=2`，**member 12—17（`test-cov` 与四员 frontend）这一遍没有跑到**，
因此本轮不声明「全门禁绿」，只声明日志里那 11 员的逐面可查。两处分寸同样入账：墙钟与退出码不是手打，
取自包装器自己的 status 文件与 stdout 日志（头部把这两条载体点名了）；`staleness-carrier.py` 自身的退出码
本轮没有任何仓内 face（它只把内层 rc 写成 `INSTRUMENT_RC=`），所以也不声明它。

## AC-9|08：reason 里那句键数不是这台探针量到的，顺手补一台「从载体重发布」的仪器

**被驳的是探针自己的文案。** HEAD 的 `scripts/quality/acceptance_item_probe.py` 里
`55,073 行里 1 个键两义` 出现 **1** 次，今天 **0** 次（`ac9-08-arm-check.txt` 的 `[5] source scan:
refuted=0 present=1`——`present` 锚的是新短语 `dwd_stock_action 实测` 确实在源里 1 处，它不是判定面）。
这台探针的读数里没有任何键计数：`measure_ac9_08` 从 C49 census 取的三个事实键是 `landed_rows` /
`passthrough_rows` / `reader_rows`，`[1]` 面逐条给出它们命中的行号与数字——`landed_rows='0'`（第 217 行）、
`passthrough_rows='55071'`（214）、`reader_rows='55073'`（212），并额外要求**数字必须出现在它所引的那一行上**，
否则算 marker 认错。同一段旧 readings 还替 55073→55071 那 2 行的差额断言了原因
（「差的那一段是一次未确认的落库写入」），这一条本轮也没量过，改成明说「若有差额，本轮没有量到它的原因，
不替它编一个」。文案改完之后，判定面没有因此变松：`[2]` 面 `repaired reading -> proven` 仍要
`landed_rows: 0 -> 55071`，`[3]` 面声明的 8 条臂逐条把干净读数打回 `gap`（`arms=8/8`），
`[4]` 面两条 null 臂篡改 `passthrough_rows`/`reader_rows` 之后读数仍 `proven`——
**这两个键只承载文案、不承载判定**，所以「reason 引了它们」不等于「判定依赖了它们」，这一点单独入档。

**台账那一格更旧。** `AC-9|08|a87bfe6f` 的 `reason` 是 C65 时代的 71 字
（`官方事件原始业务键3组重复、2组冲突`，同样是键计数），`command` 字段为空、`round=C65`、`date=2026-10-01`。
台账文案与探针文案之间**没有任何门禁在比较**：`wording_drift` 只要求判据原文出现在验收文档里，
于是 11 格 gap 文案能各自落后到 C65 而门禁全绿——这一条是本目录 `gap-reason-staleness.txt` 普查出来的成因。

**修法不是手抄。** `ledger-reason-refresh.py` 的 `reason` 逐字取载体那行 `VERDICT`，
`round`/`date` 从载体目录名与载体自己的采集时间行推导，仪器里没有一处手写轮次标签。三遍 rc 序列
`[1, 0, 0]`（`REFRESH_ARMS_RC=PASS`）：写前 `--verify-only` 读 `stale=1` 并点名
`ledger reason (71) is not the carrier's text (205)`；`--write` 打印
`WRITE AC-9|08|a87bfe6f: round C65 -> C86, reason 71 -> 205 chars, evidence_appended=True,
command_written=True` 与 `wrote docs/quality/acceptance-item-ledger.json readback_cells=1
readback_problems=0`；写后 GREEN 读 `equal=1 stale=0`。

**第一次 `--write` 的产物里抓到我自己的缺陷。** `Reading.carrier` 当时用 `path.as_posix()`，于是台账的
`evidence` 与 `command` 落进的是 `/Users/yunjinqi/…` 绝对路径——它在本机读得到、在任何别人的 checkout 里
都读不到。加 `relative()` 之后又把台账 revert 回 HEAD（`9c704e0eeba8ad18`）**重放**三遍：
重放后的 ledger diff 与重放前逐字节相同（`REPLAY_IDENTICAL=yes`），台账 sha 现为 `6263ad9e9bb85db9`，
所以入档那份是修正后的写，不是修正前写的补丁说明。写后新增的 `read_back()` 面就是把这条做成机判：
reason/round 必须按写入值读回、每条被引路径必须 repo-relative 且在盘上，否则 rc=1。

**控制臂每一条都能动。** `ledger-reason-refresh-controls.txt` 的 tally 逐字
`absent_rc=1 decline_rc=0 empty_rc=1 verify_rc=0 dry_rc=0 write_rc=0
refresh_sha=f9edb9e1e99e3af2 saved_sha=6263ad9e9bb85db9`，
`[5] sha saved=6263ad9e9bb85db9 after_dry=6263ad9e9bb85db9 after_write=94b14fc9a1ffebdd`（dry 不动字节、
write 会动，两半用的是**同一条**读数，所以「dry 什么都没改」不能由一台根本没走到写分支的仪器冒充），
`[6] rollback sha=6263ad9e9bb85db9 byte-identical=True`。`[3]` 那条负臂用的是本轮真实写过的
`ac9-08-arm-check.txt`（它确实不含 `VERDICT` 行），不是为失败造的假文件；`refresh_sha` 与被测仪器的
`[1]`—`[6]` 逐臂一起入档，因为这台控制仪是 import 那份代码跑的，档案得说自己测的是哪一版字节。

**同轮的其余三面。** `python scripts/quality/a2_check.py --files` 对这四台新仪器 + `acceptance_item_probe.py`
读 `A2 files: 4` 并 ruff/format/mypy/bandit 四项 `ok`（rc=0）；新仪器一律用 `importlib` 而不是 `subprocess`，
所以本目录的 28 处例外与全树 157 处逐行 `nosec` 计数都不受影响。`make ledger-check` 仍
`items=130 proven=112 gap=18 unreviewed=0 ticked=112`（22 组 + 19 条 §10 行）——文案改写不动 `state`，
census 不变是预期而不是巧合。`evidence_traceability` 此刻读 `untracked gaps = 8` 且 `exit gaps = 0`，
八条正是这 4 台仪器与它们的 4 份档案，提交后清除：与 `gate-run1.txt` 记的是同一条记账顺序教训。

**没做完的要写清。** 另外 10 格 C65 文案尚未刷新（`AC-10|01`、`AC-10|03`、`AC-11|02`、`AC-16|01`、
`AC-16|02`、`AC-19|01`、`AC-19|04`、`AC-1|05`、`AC-8|05`、`AC-8|06`），这台仪器已能为它们服务，
前提是每格先有**本轮**的载体而不是复用旧轮的字面量；`AC-9|08` 本身仍停在 `gap`，
因为 `landed_rows` 只能由一次经确认的 warehouse 写入补齐，那不是文案能修的形状。
