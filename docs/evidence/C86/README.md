# C86 —— 七个原本无探针的 gap 格子改由判定探针实测，AC-17|02 的例外普查改成与 bandit 同语义

本轮两件事：把登记表里 7 个「无人测量的 gap」接上真判定（`scripts/quality/acceptance_item_probe.py` 新增 7 员探针），
以及修掉一处仪器缺陷——AC-17|02 的 `# nosec` 普查按整行 grep 计数，而 bandit 按注释令牌计数，两者对不上时
探针会声称一个扫描器从未读过的例外。两处都不改判据方向：新探针一律先落在 gap，例外普查是**增加**一条检查
（切分失败的文件必须为 0），不是放宽任何一条。

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
| `ledger-seven-patch.py` | 把七格的 `state/round/date/command/reason/evidence/note` 改写为探针读数 | `python docs/evidence/C86/ledger-seven-patch.py` |

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

## 摘要重钉与门禁

（本节在门禁跑完后补写：探针模块摘要、reference-policy `--write` 的面、`--gate-check` 与 `make gate` 的 `GATE_EXIT`。）
