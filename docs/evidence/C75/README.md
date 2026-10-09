# C75 证据档案：离线验收面复测、权利面登记与门禁第 3 员的转绿（2026-10-09）

## 0. 本轮定位

C75 不新增功能面，只做六件有后果的事：

1. 把本轮改动之后的**离线验收自证面**重新算一遍（AC2 25 例 + 验收项 115 探针），并把读数写进档案；
2. 把权利登记面与迭代2 README/前置条件清单里**引用的数字**逐个回到活注册表复算，纠正被复算否掉的旧数；
3. 清掉门禁第 3 员（`secret-check`）本轮的两处实测红：一处 census 里的引擎字段赋值被读成凭据，修好后又浮出 11 条 H.15 列名 + 2 条我自己夹具载体 —— 用**20 条臂的差分**证明四条新增豁免各自只静默什么，而不是"扫出来安静了"；
4. 清掉修好第 3 员之后才暴露的第 2 员（`zero-dep-check`）红：我把 `acceptance_item_probe.py` 的允许面钉子改了 3 行，于是参考策略里那条 88 项摘要不再匹配磁盘字节 —— 这条漂移用**仓库自己的判定函数**复核成"同一批准内容下的摘要重钉"，不是新的批准；
5. 清掉第 12 员（`test-cov`）里唯一的一条红：`AC-1|08` 的真实登记表钉子还写着 14 行，而本轮合法地登记了第 15 行（美联储理事会）。这不是把判定放宽，而是把**已经决定的那一行的实质**读回来 —— 复算见 `ac1-08-rights-registry-repin.txt`；
6. 提交前清掉两个会伤害后续轮次的结构性隐患：一个把三个工作树验收面永久锁在 deferred 的未忽略轮转日志目录，和一个会把合成凭据变成永久历史命中的夹具写法（`§6`）。

`make gate` 第一次运行在第 3 员 `exit 2` 停住（见 `gate-run1-member3-red.txt`）；第二次修好后停在第 2 员 `zero-dep-check`（那次第 3 员已 `no leaks found`；第 2 员的复算见 §3）；第三次停在第 5 员 `evidence-traceability`，原因是这两个新档案文件当时还未跟踪 —— 每一次停止都比上一次更靠后，这正是"更早的红的掩蔽作用"的读数。完整 17 员的最终逐员读数由转绿或最后那一次运行提供（`gate.txt`）。

## 1. 判定面读数（每条都有产生它的命令）

| 面 | 读数 | 产生方式 |
| --- | --- | --- |
| AC2 自证 | 25/25 例 `closure=holds_offline`，反事实 153 breaks / 153 flipped，`SELF_TEST PASS failures=0`，`AC2SELF_RC=0` | `offline-acceptance-faces.txt` |
| 验收项自证 | 115 个探针全部走到判定，751 条 counterfact 全部把干净读数打回 gap，`ITEMSELF_RC=0` | 同上 |
| 注册/路由面 | capabilities=49，`source="auto"` 可路由=23，模型描述符=16（唯一模型 16），带能力的源=11，leg 域=36 / leg=49，`canonical_capabilities()`=37 | `status-faces.txt`（脚本 `measure-status-faces.py`） |
| 任务面 | ledger 350 行 / 32 个 provider / 202 个唯一上游模型；`implementation_task_status` = DEV_DONE 5 + IN_PROGRESS 13 + NOT_RUN 332；`live_verification_status` = NOT_RUN 350；`scenario_status` = NOT_ASSESSED 350 | 同上 |
| 权利面 | `rights_status` = NOT_REGISTERED 265 + EXISTING_SOURCE_REVIEW_MODEL_PENDING 85 | 同上 |
| 迭代1 AC 台账面 | `items=130 proven=112 gap=18 unreviewed=0 ticked=112`，22 组 + 19 条 §10 行同时对账 | `gate-full-2` 的第 4 员读数（`gate.txt` 同一命令） |
| 零依赖面 | walked 649 个文件（`opendata`=322、`opendata/data/providers/akshare/_vendor`=325、`opendata_client`=2），python 3.11，无新增上游引用；基线冻结 0 条引用 | `zero-dep-and-inventory.txt` |
| 参考策略面 | 88 条登记项，磁盘摘要匹配 88、漂移 0、缺文件 0；`judge_problems=0` 且与自建 census 一致 | `reference-policy-pin-audit.txt`（脚本 `reference-policy-pin-audit.py`） |
| 清单面 | `provider_inventory.py --check-manifest` RC=0（本轮先读出不一致、复冻后转绿） | 同上 |
| 全量套件面 | `1 failed, 21275 passed, 86 skipped in 599.78s` —— 唯一的红是 §4 那条行数钉子；重钉后该文件 42 passed | `gate-full-3.txt`、`ac1-08-rights-registry-repin.txt` |

DEV_DONE 的 5 行是 cboe `AvailableIndices`/`IndexSearch`/`IndexConstituents` 与 federal_reserve `MoneyMeasures`/`TreasuryRates`。权利面里 85 行是"源级已登记、模型级仍待评审"，265 行仍是逐源未登记 —— 这两个数是 CSV 的逐行读数，源级登记不改动它们（口径写在 `前置条件与决策清单.md` 的标签词表一节）。

## 2. 门禁第 3 员：两条原因、四次读数，差分从 15 臂扩到 20 臂

### 2.1 第一处：census 里那句 `rows_pointer=refRates`

`gitleaks detect --source . --config .gitleaks.toml --redact`（8.30.1，命令固定在 `docs/quality/secret-scan.json`）扫 300 commits、134.55 MB，报 `leaks found: 1`。那一条是：

- 文件 `docs/迭代计划/迭代2-统一Provider架构与全量能力补齐/census-sec-tmx-fed-gov-finra.json`
- 规则 `generic-api-key`，secret `rows_pointer=refRates`，entropy 3.6537566，commit 8a35e9aca5ce

`rows_pointer` 是引擎字段名、`refRates` 是公开发布表段落标识，两者都不是凭据；`generic-api-key` 把行首的词 `keys` 当成键名、把后面的赋值当成值。**"文件里没有别的凭据"这句话由扫描本身承担**：修复前全历史只有这 1 条未豁免命中，所以它不依赖任何人手抄的正则。

处理方式是**字面量级**豁免（`.gitleaks.toml` 里 `generic-api-key` 的 `[rules.allowlist]` 第三条 `^rows_pointer=refRates$`），并同步把它写进被审计的钉子 `scripts/quality/acceptance_item_probe.py::GENERIC_API_KEY_ALLOWED_REGEXES`，使 AC-1|09 的允许面审计与配置文件同形 —— 否则豁免就长在监视器之外。

差分（`gitleaks-counterfactual.py` / `.log`，每条臂声明"必须报出的规则 id 集合"而不是条数；这一小节先 15 条臂，§2.2 把第 4 条豁免加进来后是 20 条）测出：

* 移除第三条正则后该行重新被报出（臂 2）—— 没有这条，臂 1 的"安静"也可能只是因为规则本来没碰这个文件；
* 末尾多一个字符 `refRatesX` 仍被报出（臂 3）—— 豁免是那个字面量，不是前缀、不是形状；
* 同一个槽位换成 64 位十六进制凭据仍被报出（臂 7）；换成 `ghp_` token 由 `github-pat` 报出（臂 4/5）—— 豁免不能替任何凭据形状背书；
* C36 的 SDMX 豁免与 C73 的 path→sha256 成对豁免各自仍按自己的形状生效（臂 8/9、13/14），本轮的编辑没有把它们放宽。

### 2.2 第二处：修好第一条之后，run #4 从 1 条命中变成 13 条

第一次修复后重跑完整门禁（run #4，当时 HEAD `5e7c81a2e39d`、`UNCOMMITTED_PATHS=0`），第 3 员不是回到绿，而是报 `leaks found: 13`。**"扫出来更吵了"不是失败，是读数变完整**：这 13 条里没有任何一条来自别人写的文件，全部是本轮自己刚提交进去的字节。逐条读那份报告（`/tmp/c75/leaks-4.json`，要点抄下）：

| 条数 | 位置 | 规则 | entropy | 列 / 标签 |
| --- | --- | --- | --- | --- |
| 11 | `opendata/data/providers/federal_reserve/specs.py:193-203` | `generic-api-key` | 3.6644979（11 条完全相同） | 38-64 或 39-65（随行内字段名长度变） |
| 1 | `docs/evidence/C75/gitleaks-counterfactual.py:82` | `generic-api-key` | 4.6977305 | 2-77，命中的是 base64 载体本身 |
| 1 | 同一行 | `github-pat` | 4.921928 | 21-76，`Tags: decoded:base64, decode-depth:1` |

两件事由此分开处理，而且第二件是**我仪器的错，不是配置太松**：

1. **11 条 = 本轮新写的 H.15 期限档列名**。`generic-api-key` 把字段名 `source_key` 当键名、把值当键值；那些值是国债收益率发布表的列标识（`RIFLGFCM01_N.B` … `RIFLGFCY30_N.B`），是引擎拿去匹配表头的 join key，不是凭据。豁免走 `generic-api-key` 允许面的**第四条字面正则**，并且是**枚举这十一个 id**，不是给 `source_key` 一个值类 —— 后者会把"有人真把凭据写进 ColumnSpec"一起静默。同时把这条正则逐字镜像进 `GENERIC_API_KEY_ALLOWED_REGEXES`，让 AC-1|09 的允许面审计与配置文件同形。
2. **2 条 = 差分夹具里那个假 token 的载体被识破了**。上一稿为了"不让合成凭据进入历史"把它 base64 编码；gitleaks 8.30.1 在匹配前先解一层 base64，所以同一行报了两次：编码字面量被 `generic-api-key` 按高熵串命中，解码后的值被 `github-pat` 命中 —— 报告里那对 `decoded:base64 / decode-depth:1` 标签就是这次否证自身的载体。现在改成四段九字符字面量运行时拼接（`_PAT_PREFIX` + `_PAT_BODY_CHUNKS`），源码里 `grep -cE 'ghp_[0-9a-zA-Z]{36}'` → `0`（exit 1），而装配值仍是规则能命中的形状（臂 4/5/19 都在扫它）。带这 2 条命中的 commit 用 `git reset --soft HEAD~1` 撤回重做，**不让自己的误报进入历史**：一个每轮都要重新解释一遍的修复不算修复。

第四条豁免的边界与被否证的说法都由 20 臂那版差分承担（`gitleaks-counterfactual.log`，`COUNTERFACT PASS: 20 arms agreed with their declared rule sets`、`COUNTERFACT_RC=0`）：

* 臂 16 一次提交真实文件里全部 11 行 → 无命中；臂 17 同一夹具只删第四条正则 → `generic-api-key` 复报。这一对是让臂 16 成为度量而不是沉默的关键；
* 臂 18 把 `_N.B` 尾字符改成 `_N.X` → 复报：豁免是那十一个 id 的枚举，不是前缀或形状；
* 臂 19 同一槽位放 token → `github-pat` 报；臂 20 同一槽位放 64 位十六进制摘要 → `generic-api-key` 报：字段不能借豁免 smuggle 凭据；
* 影响面是从树里算出来的，不是断言的：该文件 11 行、11 个不同值、全部被这条正则匹配 True；跨所有 `opendata/data/providers/*/specs.py` 共 35 个 `source_key` 值（29 个不同），**被匹配又不属于那 11 个 Fed 标识的 = 0**。

修复后第 3 员在 HEAD `8a35e9a` 上读 `300 commits … no leaks found`（`secret-scan-before-after.txt`）。这条读数有个必须写明的范围：那 11 行 H.15 只在暂存区、不在历史里，所以**这次运行看不见它们**；真正检验第四条豁免对不对得上那 13 条命中的是提交之后扫 301 commits 的那一次（`gate.txt`）。

**测出来的既有边界（不是本轮造成的）**：C73 的豁免是 `regexTarget = "line"`，所以与成对行**同一行**上共写的凭据会被**所有规则**静默。臂 11/12 双侧量化了它 —— 同一行共写：有该块时读 0 条、去掉该块后 `github-pat` 与 `generic-api-key` 都报；token 单占一行：仍然报。树内此类共现行实测 3 行（`docs/evidence/C65/` 的 `HASHES {…}` 记录，6+1+6 对），三行都不含凭据。收紧规则会重开那三行已归档记录，而那不是本轮该改的历史，因此本轮的处置是**把它写成度量过的边界**并留下双侧对照，不放宽也不假装没有。

### 2.3 这条豁免逼出的质量面：那十一个列名在树内没有任何载体

为了写第四条豁免的正则，我必须回答"这十一个 id 是从哪儿来的"。答案是它们**不能在本树里被复算**，而这正是豁免注释里必须说清的事 —— 否则我只是在给一串无法反驳的字符发许可证。三条读数：

* `ls providers` → `No such file or directory`；全树 `find -type d -name record` → 0 项。specs.py 旧稿引用的那份抓包根 `providers/federal_reserve/tests/record/http/test_federal_reserve_fetchers/*.yaml` 在这里不存在；
* 装好的只读预言机里也没有：`openbb_federal_reserve` 1.6.2 下 20 个 `.py`，含 `RIFLGFC` 的文件 **0 个**；
* 于是"header 单元格、行数、`ND` 计数"那批数字全部是**这个文件携带的断言**，不是可在此重测的字节。测试也不能否证它们：它把同样十一个名字手打进自己的 fixture，所以它证明的是 spec→行的映射，与发布内容无关。

还有一个不对称让这件事变成风险而不是洁癖：上游对这张表是**按位置命名**的（`treasury_rates.py:96` 用 `df.columns = ["date"] + maturities` 覆盖，`maturities` 在 `:18-29`），它从不读我们 `source_key` 拿去匹配的那些表头单元格 —— 所以一个写错的列名不会导致拒绝，只会导致 null 列。处置（本轮已做）：把 specs.py 的 docstring 按"有载体的事实 / 只有断言的事实"重新分栏，请求侧的每条都注明是从安装好的扩展重读的（含行号），响应侧明确写成不可在此复算；并把"给这些列名一个带摘要的树内 fixture（抓包归档）"记为待办 —— 那需要访问上游的授权，本阶段没有。`live_verification_status` 因此保持 NOT_RUN。

同一批 census 的引用根也顺带读出漂移，而 #35 把这条漂移**解释掉了**而不是记下就算：计数规则是"递归走每一行的每个字符串值，按字符串值计"。改写前 15 个字符串值含 `providers/federal_reserve/`，改写后 16 个；但这个子串同时命中两种完全不同的根，所以必须拆开读，否则 15→16 会被读成"引用变多了"这种毫无意义的结论：

* **安装根形状** `providers/federal_reserve/openbb_federal_reserve/...`：改写前 13 个、改写后 13 个，一个没动 —— 其中 **12 个**是 `…/models/<module>.py:NNN` 形式的 evidence 行（我早先那句"13 个是 models/*.py:NNN 形式"把这两个数混了，按形状重新读数它只能是 12），`record/http` 出现 0 次；
* **仓库根形状** `opendata/data/providers/federal_reserve/specs.py`：1 个 → 3 个，多的两个是这次重读给 SOFR、OvernightBankFundingRate 写的 `audited.basis`；
* **第三种写法**：1 个 → 0 个。它不是引用，是 EFFR 老 notes 里一句**谈论这个形状本身**的散文 —— 原文尾部写着 `the cited providers/federal_reserve/... path resolves outside the repo root`，也就是说这条命中是"我们在描述一根无法解析的根"，被本轮按安装根绝对位置重写的 notes 取代了。

13 + 1 + 1 = 15，13 + 3 + 0 = 16 —— 三个桶各自可复算，总数只是它们的和。安装根本轮落到绝对位置：`/Users/yunjinqi/opt/anaconda3/lib/python3.11/site-packages/openbb_federal_reserve/models`，版本 1.6.2，`docs/evidence/C75/fed-percent-census-recheck.py` 每一遍都先把这个根和版本打印出来，再从该目录逐行读出 `sofr.py` 的 before-validator（`:34-50`，除法 `:49`）、`overnight_bank_funding_rate.py`（`:47-61`，`:60`）、`federal_funds_rate.py`（`:63-81`，`:80`）与三处 `return sorted(results, key=lambda x: x.date)`（`:108 / :122 / :141`）；引用是读出来的，不是抄进文件的。同一遍重读改写了 3 行的判定：SOFR 与 OvernightBankFundingRate 由 `true` 降为 `false`（缺 `published_value_rescale` 与 `client_side_sort`，映射到登记表即 `columns.rescale`、`rows.order_limit`，两行状态都读作 `absent`），FederalFundsRate 仍是 `false` 但需求清单由 3 项收正为 2 项 —— `client_side_column_drop` 不是阻塞，因为它映射到 `columns.select`，而那正是引擎已交付的能力（`normalize_record` 只按声明列取值，未声明的键根本到不了行模型）。本文件里的提及数随之变动：`published_value_rescale` 2→4、`client_side_sort` 1→3、`client_side_column_drop` 1→0。路线图同一遍重生成后读到：150 行 / 150 个唯一 task_id 的分母不变，`expressible_today` 4→2，pending 146→148，`--check` PASS。这些数都是 `--write` 前后各读一次同一脚本得到的实测：改写前的整份文件已归档为 `docs/evidence/C75/census-before-rewrite.json`，桶划分与差值脚本是 `docs/evidence/C75/census-cite-buckets.py`，它这一遍的输出留在 `docs/evidence/C75/census-cite-buckets.txt`（复算命令：`python3 docs/evidence/C75/census-cite-buckets.py docs/evidence/C75/census-before-rewrite.json docs/迭代计划/迭代2-统一Provider架构与全量能力补齐/census-sec-tmx-fed-gov-finra.json`）。

## 3. 门禁第 2 员：我自己造成的摘要漂移，用仓库自己的判定器复算

第 3 员转绿后，完整运行停在第 2 员：

```
FAIL: reference policy is unusable: stale sha256: scripts/quality/acceptance_item_probe.py
```

原因可追溯到本轮自己的编辑：我把 `.gitleaks.toml` 的 `generic-api-key` 允许面逐字镜像进
`GENERIC_API_KEY_ALLOWED_REGEXES`（+3 行：2 行注释 + 1 行字面正则），而 `docs/quality/akshare-reference-allowlist.json`
给每个被批准含 `akshare` 引用的文件钉了一个磁盘 SHA-256。`--update` 也救不了：它先调用
`load_reference_policy()`，策略不可用就直接拒绝冻结（`verify_no_akshare.py:1052`）。

关键是**不能把这条红当作记账问题绕过去**。摘要钉是"已评审内容仍是已评审内容"的唯一篡改证据，
所以修复方式必须由测量决定 —— 重钉（hash refresh）还是重新评审（review）。`reference-policy-pin-audit.py`
按 C72 §3 定下的判据逐项算，88 条全算，不只算报错那条：

* `digest_drift=1`、`missing_from_tree=0`，且我自建 census 的漂移路径集合与 `verify_no_akshare.reference_policy_problems`
  的输出**完全一致**（`census_agrees_with_judge=True`）—— 仪器看到的是同一棵树；
* 那条 pin 命中 HEAD `8a35e9a` 的 blob，`akshare` 出现行 pinned=124 / live=124 且**逐行多重集相同**，
  相对 pin 只多那 3 行，注册 `purpose` 仍描述该文件 —— 这就是允许重钉的条件；
* 双侧对照：内存里伪造一条 pin，判定器必须恰好报 1 条且只报那条（`control_corrupt_flags_only_target=True`），
  所以上面的 `judge_problems` 是活的读数，重钉后的 `0` 也不是沉默；
*  hypothetical 全量重钉臂（`control_hypothetical_full_repin_problems=0`）只用于证明"这棵树与策略之间除摘要外没有别的差异"；
  实际写入的修复只重钉**被审计那一条**的 `sha256` 并在 `reviewer` 里带上这次审计，其余 87 条一个字节都没动。

重钉后三臂：审计 `judge_problems=0 / live_digest_matches=88`、普通扫描 `OK: 649 file(s) walked … frozen baseline: 0`、
`--self-test` 仍"13 violations detected, 4 compliant samples clean, scan-surface guards bite in both directions"。
本轮不需要重冻基线：走到的文件 census 已经等于冻结值（322+325+2=649），这与 C72 不同 —— 那里树实际多了 7 个第一方文件。

### 3.1 第二次漂移：同一个文件我又加了 4 行，审计因此多了两个臂

§2.2 的第四条豁免又要镜像进 `acceptance_item_probe.py`，同一条 pin 第二次离开磁盘字节。这次重跑审计读到的不是同一个形状：

```
judge_problems=1 / digest_drift=1
  DRIFT scripts/quality/acceptance_item_probe.py: pin matches no revision -> needs a review, not a hash refresh
VERDICT: at least one drift changes what is allowed; review before re-pinning   （RC=1）
```

这条读数**是我的审计器的缺陷，不是树的缺陷**：上一轮的重钉字节进了暂存区却没进任何 commit，而审计只按 `git log -- <path>` 找载体，于是"pin 命中某个 revision"这个判据在这种状态下永远不可能成立。处理方式是给审计加第二个载体而不是放宽判据 —— `staged_blob()` 读 index blob，pin 等于它时打印 `pin == INDEX(staged)`，并且**被批准的实质对照（akshare 出现行的多重集 pinned vs live）照旧跑在这条 entry 上**。加上之后同一份数据的读数是：

```
  DRIFT scripts/quality/acceptance_item_probe.py: pin == INDEX(staged) occurrences_pinned=124 occurrences_live=124 identical=True
pin_resolved_in_staged_blob=1
pin_resolved_nowhere=0
VERDICT: every drift is the same allowed occurrences under moved bytes            （RC=0）
```

重钉之后（`sha256` 换成磁盘字节，`reviewer` 追加这次复核）：`judge_problems=0 / live_digest_matches=88 / digest_drift=0 / missing_from_tree=0`，成员 2 读 `OK: 649 file(s) walked … frozen baseline: 0`。

第二个新臂是被这次读数逼出来的：伪造一条 pin 的对照跟着漂移列表走，漂移修完列表就空了，于是它去改 `.env.example`（`control_target=.env.example`）—— 一个与本轮无关的 entry，此时打印的 `judge_problems=0` 关于我改过的文件**什么都没说**。新增 `control_rearm_*` 只在内存里伪造 `acceptance_item_probe.py` 那一条，判定器必须恰好报出这一条路径，否则 VERDICT 直接返回 1：

```
control_rearm_target=scripts/quality/acceptance_item_probe.py
control_rearm_problems=1
control_rearm_flags_only_that_path=True
```

三条行数钉子（同一个文件，逐次量出来）：HEAD `16127` 行 → index `16130`（+3）→ 工作树 `16134`（+4）；akshare 出现行三种字节下都是 `124`。另外两个"不在监视面里"的事实也是量出来的，不是 assumed：`.gitleaks.toml` 与 `opendata/data/providers/federal_reserve/specs.py` 都不在 `akshare-reference-allowlist.json` 的 88 条里（`'federal_reserve/specs.py' in policy` → False），也不在 `docs/quality/baseline.json` 里（`federal_reserve` 出现 0 次）。

一处口径要写在这里，因为它差点变成错的数：审计的 `occurrences()` 用 `casefold()`，所以是 124；我手算"含 `akshare` 的行"得到 118。差 6 行是大小写变体，不是漏测 —— 引用这类数只能引审计打印出来的那个，不能引用我另算的谓词。

## 4. 门禁第 12 员：一行行数钉子挡住了一个已经决定完的登记行


第三次完整运行停在第 12 员，且整个套件只有这一条红（verbatim，`gate-full-3.txt`）：

```
________ test_ac1_08_real_registry_measures_complete_restricted_review _________
tests/test_acceptance_item_probe.py:286: in test_ac1_08_real_registry_measures_complete_restricted_review
    assert facts["rows"] == "14"
E   AssertionError: assert '15' == '14'
```
```
=========== 1 failed, 21275 passed, 86 skipped in 599.78s (0:09:59) ============
```

红的是**测试里的字面量**，不是判定面。用探针自己的度量函数在真实仓库上重算（`ac1-08-rights-registry-repin.txt`）：

```
{'table_valid': 'yes', 'rows': '15', 'dated': '15', 'undecided': '0', 'unlinked': '0', 'responsible_missing': '0', 'uncovered': '0'}
verdict_state= proven
```

第 15 行是本轮新登记的美联储理事会（`git log` 的上一轮同类重钉是 `0164751`：cboe 两个域注册时也是登记文件与钉子同批改动）。因此修复方向不是把 14 改成 15 就交差，而是**把那一行的实质读回来**：`rows[15]` 的数据源、状态=已复核（受限）、复核日期 2026-10-09、条款链接、以及七个栏位里的关键论断（public domain 原文引用、须注明美联储为来源、`unless otherwise indicated`、本行只覆盖 `datadownload/Output.aspx` 两张发布表下载包、责任人）逐栏断言。顺带纠出一处更早的过期陈述：登记表原文写"不覆盖 cboe 其余 9 个上游模型"，而 `IndexSearch` 注册后清单实测 11 行 = 3 DEV_DONE + 8 NOT_RUN，所以是 8 个。

**判定器没有被放宽，这条是被测出来的**：本轮 `scripts/quality/acceptance_item_probe.py` 的唯一暂存改动是允许面那 3 行镜像，

```
$ git diff --cached -U0 scripts/quality/acceptance_item_probe.py | grep -cE "^[-+][^-+]" 
3
$ git diff --cached -U0 scripts/quality/acceptance_item_probe.py | grep -E "^[-+][^-+]" | grep -icE "ac1_08|measure_ac1|judge_ac1"
0
```

即 `measure_ac1_08` 与 `judge_ac1_08` 的字节一个都没动 —— AC-1|08 的"完整但受限"判据仍是同一条，改的只是围绕它的期望值。重钉后 `python3 -m pytest tests/test_acceptance_item_probe.py -q --no-cov` → `42 passed in 9.67s`。

## 5. 我的仪器缺陷（按原样留档，不删）

`counterfactual-draft-failures.log` 保留了两版失败的差分：

1. 手写夹具版的臂 [5]/[10] 失败 —— 全 `a` 的"摘要"熵为 0，任何规则都不可能命中；一行式 `{"src/a.py": "<hex>"}` 不满足该规则需要的关键字邻近；而只删 `[[allowlists]]` 里的一条正则会让 gitleaks 以"块必须至少含一项检查"直接退出，对照必须整块移除。
2. 真实行版本的臂 [4]/[8] 失败两次，源于同一个**错两次**的理论：以为 `generic-api-key` 的值字符类只收小写。最终臂 [6] 定了性 —— 同一串 36 字符去掉 `ghp_` 前缀就被报出，所以那个规则跳过的是前缀（`ghp_` 归 `github-pat` 所有），与大小写无关。
3. 本轮更早还有一次假警报：我自己的复算脚本把判定值与手打的大写字面量 `"GAP"` 比较，于是 42 条反事实读成"没有翻回 gap"，而仓库自带的 `--self-test` 在同一棵树上给 115/115、751/751。教训是仪器的字面量必须从被测模块导入，而不是抄。

这三条都是差分而不是口头判定的产物：安静只会掩盖它们，逐臂声明期望值才会把它们打印出来。

第 2 员的修复又留下三条同类的仪器缺陷，一并按原样记：

4. 新审计脚本第一次把仓库根算成 `Path(__file__).resolve().parents[2]`（`docs/evidence/C75/x.py` 的根是 `parents[3]`），于是
   `ModuleNotFoundError: verify_no_akshare` —— 这是"仪器自带的 off-by-one"，不是被测面的失败；把它写死成绝对路径可以蒙过这一关，所以留的是可移植的 `parents[3]` 并把 `repo=` 打进读数第一行，让根路径成为一个可被反驳的字段。
5. 同一条 pin 的允许面有 4 个平面在监视：`ruff` 先报 `D103`×5 与 `S603`（`# nosec B603` 只安抚 bandit，ruff 要 `# noqa: S603`），
   `ruff format` 要拆开那个跨行 `print`，`mypy --strict` 报两处 —— 其中 `for path in missing:` 复用了一个已推断为 `Path` 的名字，
   是真实的类型混用而不是风格。`bandit` 这次另报 `B404`（`import subprocess`），而 `a2_check._bandit()` 不按严重度过滤任何 result，
   所以它一定会红：这一条只有直接跑 bandit 才看得见 —— `make gate` 前三次分别停在第 3、第 2、第 5 员，从没跑到第 8 员（`a2-check` 在 Makefile 的 `gate` 目标里排在 brand/zero-dep/secret/ledger/evidence-traceability/js-points/loguru 之后）。
6. 修完后重跑审计，输出与修复前那份逐字节相同（`diff` 无差异），档案里的两段 verbatim 读数因此仍然成立 —— 注释与 docstring 不该改变测量，这条是把它当成断言来验的。
7. 同一件事在第二次注释修正（把审计脚本首行的 `member 3` 改成 `member 2`，因为 `zero-dep-check` 才是 Makefile `gate` 目标里的第 2 员）后又验了一次，但**这次不能写"逐字节相同"**：档案里的 `.txt` 含修复前（`judge_problems=1`、`digest_drift=1`、`pin == INDEX(staged)`）与修复后（全 0 + re-arm 臂）两段读数，单次运行只产出后一段。正确的关系是**子集**：新运行 17 行读数体全部落在已归档的 37 行里，`only-in-new=[]`，所以没有一条测量因这次编辑而变。断言要按档案的实际形状写，否则"identical"这句话本身就成了不可复算的说法。

## 6. 提交前清掉的两个结构性隐患

这两条都不是本轮功能的失败，而是**本轮之前就在树里、会一直伤害后续轮次**的东西。都在提交前用双侧对照测过。

**（1）`logs/` 未被忽略，把三个时刻面永久锁在 deferred。** 判定的"工作树是否安静"用的是 `git status --porcelain --untracked-files=all`（`acceptance_item_probe.py:811`、`:1950`），而第 8 条读数证实它此刻就在延迟这三格：

```
  - 台账↔读数：agrees=112, unflipped=0, open=0, deferred=3, stale-proof=0（共 115 格有读数）
  - deferred（时刻面，等工作树干净再判）：AC-17|08、AC-1|03、AC-1|10
```

`logs/app.log` 被 `.gitignore:61` 的 `*.log` 覆盖，但 loguru 轮转出的 `logs/app.2026-09-29_14-26-14_289936.log.gz` 不匹配 `*.log`。双侧对照（临时 git 仓库，先写 `.gitignore` 再落文件，否则测的是空忽略表）：

```
--- without the logs/ line (the tree before this round) ---
   check-ignore rc=0 logs/app.log <- .gitignore:61:*.log	logs/app.log
   check-ignore rc=1 logs/app.2026-09-29_14-26-14_289936.log.gz <- (no match)
   status --porcelain --untracked-files=all -> dirty=2 ['?? .gitignore', '?? logs/app.2026-09-29_14-26-14_289936.log.gz']
--- with logs/ (shipped) ---
   check-ignore rc=0 logs/app.log <- .gitignore:68:logs/	logs/app.log
   check-ignore rc=0 logs/app.2026-09-29_14-26-14_289936.log.gz <- .gitignore:68:logs/	logs/app.2026-09-29_14-26-14_289936.log.gz
   status --porcelain --untracked-files=all -> dirty=1 ['?? .gitignore']
```

差量就是那个 `.gz`：加 `logs/` 前它是 `??` 行，加完它不再是。这一条本身也错两次才被测对：第一版把 `check-ignore` 的参数写成裸文件名（少了 `logs/` 前缀），第二版忘了在临时仓库里写 `.gitignore` —— 两次都读出"两侧一样"，那是仪器的沉默而不是结论。

**（2）差分夹具里的合成凭据会从"本轮的一次测量"变成"永久历史命中"。** 第 3 员扫的是**已提交历史**，所以 `gitleaks-counterfactual.py` 里那个 `ghp_` 形状的假 token 一旦被提交，就会成为之后每一轮的 `github-pat` 历史命中 —— 正是本轮在清理的那类误报。第一版处置是**错的**：把 token base64 编码，以为编码字面量不是凭据形状。run #4 用两条命中否证了它 —— 同一行先被 `generic-api-key` 按编码字面量报出（entropy 4.6977305），再被 `github-pat` 按解码后的值报出（entropy 4.921928，`Tags: decoded:base64, decode-depth:1`），因为 gitleaks 8.30.1 在匹配前会解一层 base64。**编码不是载体。** 现行处置是运行时装配：源码里只有前缀常量与四段九字符字面量（`_PAT_PREFIX` + `_PAT_BODY_CHUNKS` → `FAKE_TOKEN`），完整串只存在于进程内存。这不是削弱对照，两侧都测了：

* 源码里不再存在可被那条规则匹配的完整串 —— `grep -cE 'ghp_[0-9a-zA-Z]{36}' docs/evidence/C75/gitleaks-counterfactual.py` → `0`（exit 1）；
* 装配出来的值仍是规则能命中的凭据形状 —— baseline 阶段在默认规则集、移除全部允许面下扫它，`github-pat` 照报（臂 4/5/19 都期望并读到它）；
* 带那 2 条命中的 commit 用 `git reset --soft HEAD~1` 撤回重做，没让它进历史 —— 一个每轮都要重新解释一遍的修复不算修复。

重跑 20 条臂：`COUNTERFACT PASS: 20 arms agreed with their declared rule sets`、`COUNTERFACT_RC=0`（`gitleaks-counterfactual.log` 是这次运行，头部记了为什么改成字面量分段装配）。四个平面在同一条文件上重测：`ruff` All checks passed / `ruff format --check` already formatted / `mypy --strict` no issues / `bandit -c bandit.yaml` results=0 —— 其中 ruff 那一条是 `E501` 行宽，我加解释注释时把它写长了；这一版也不再需要 `# nosec B105`：`grep -c 'B105' docs/evidence/C75/gitleaks-counterfactual.py` → `0`，因为被打上密码味名字的已不是字符串字面量。

## 7. 复算方式

```
python docs/evidence/C75/gitleaks-counterfactual.py        # COUNTERFACT PASS: 20 arms
python docs/evidence/C75/reference-policy-pin-audit.py     # 88 条钉子的漂移 census + 双侧对照
python docs/evidence/C75/measure-status-faces.py           # 表 1 的注册/任务/权利面
python scripts/quality/ac2_case_probe.py --self-test       # 25/25、153/153、RC=0
python scripts/quality/acceptance_item_probe.py --self-test # 115 probes、751 flips、RC=0
python scripts/codemod/verify_no_akshare.py                # 成员 2：649 walked、baseline 0
python scripts/codemod/verify_no_akshare.py --self-test    # 成员 2：13 planted violations 全中
python scripts/quality/secret_scan_check.py                 # 成员 3：no leaks found
make gate                                                   # 完整 17 员，见 gate.txt
```

## 8. 仍关闭中的面

* 全量测试与合并覆盖率：本轮只复测受影响文件（122 个测试文件、24 个、34 个分别通过），新树的全量收据以 `gate.txt` 为准；
* AC-1|05（隔离栈）与 AC-10|03（源级复认证）都卡在需要授权的动作上：起 Docker、以及为 bls/cboe/federal_reserve/oecd/sec 实做一次 `provider_source_review_evidence_build.py --findings` 源评审；
* 13 行 IN_PROGRESS 与 332 行 NOT_RUN 仍在分母里，`live_verification_status` 350 行全部 NOT_RUN；
* akshare `upstream.lock` 的 `manual_edits=true`、AC-13 `dwd_*` 落库、AC-15 DROP、`dq_diff_report` DDL、8 张缺 `p2027` 分区的表、磁盘水位告警口径：都要用户确认，本轮未动；
* 发布面（SOURCE_VERIFIED / DEPLOYMENT_VERIFIED）按约定另行安排，当前离线阶段仍 INCOMPLETE，发布仍 NO-GO。
