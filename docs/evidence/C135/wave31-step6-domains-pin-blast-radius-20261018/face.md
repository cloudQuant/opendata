# wave-31 step 6：`domains.yaml` 的钉死半径与 tmx 剩余 16 行的落地前置条件

结论（一句）：**落地 tmx 任意一行剩余 `implement_missing_model`，都要改 `opendata/data/domains.yaml`；而该文件被全部 291 条已验收行的 1455 个 pin 槽引用，改一次 = 全树重新生产 + 重新签署。** 实测半径 `291/291 行`，不是估计。
本步未改任何产品代码，平面 1 仍为 `291/350`，身份集 `5168e1e7…` 未动。

---

## 1. 为什么这一步是"要裁决"而不是"要写代码"

判定器把每条 claim 的每个 pin 路径拿工作树字节重算：
[iter2_model_evidence.py:625](../../../../scripts/quality/iter2_model_evidence.py#L625) `current != after → {group}_pin_{i}_current_hash_drift`，并把该行判为 stale。
`domains.yaml` 是生产者写进**每条** phase claim 的 `contract` 组的固定成员，所以它是全树共享前提，而不是某个 provider 的本地契约文件。
"要不要为一个 provider 的一行付全树重签的代价"属于会动到 291 条已验收证据行的决定，不是波内可以自行豁免的事——豁免它等价于放宽判定器，而判定器不在我的处置权内。

## 2. 仪器与复算

三台，都在本 bundle 的 `probes/`，用 gate 解释器 `/Users/yunjinqi/opt/anaconda3/envs/py313/bin/python` 直跑，均只读（第三台 `staged_payload_credential_scan.py` 扫的是 index，见 §10；三台的 A2 静态平面见 §11）：

```
$PY docs/evidence/C135/wave31-step6-domains-pin-blast-radius-20261018/probes/domains_pin_blast_radius.py
$PY docs/evidence/C135/wave31-step6-domains-pin-blast-radius-20261018/probes/tmx_residue_transport_map.py
```

两台都 import 仓内判定器（`scripts.quality.iter2_model_evidence`）与仓内见证模块的 seam 常量（`iter2_witnesses/tmx.py` 的 `_GQL_HOST`/`_SEAM`），不手抄判定语义。
模拟"改完之后的字节"只写在内存与 `artifacts/`，磁盘上的 `domains.yaml` 一个字节都没动（见 §6）。

## 3. 面 1：钉死半径（`runs/domains_pin_blast_radius.txt`，4/4 臂绿，exit=0）

| 读数 | 载档行 |
| --- | --- |
| `population: inventory=350 claims=1565 reviews=291 status_counts={'INCOMPLETE': 22, 'OFFLINE_ACCEPTED': 291, 'UNREVIEWED': 37}` | `:1` |
| `accepted: judge_offline_acceptance_count=291 derived_rows=291 derived_claims=1455 rows_x5=1455` | `:2` |
| `census: accepted_claims=1455 slots=20744 distinct_paths=589` | `:3` |
| `shared_path slots=1746 rows=291 path=scripts/quality/iter2_claims_fixtures.py` | `:4` |
| `shared_path slots=1746 rows=291 path=scripts/quality/iter2_claims_probe_runner.py` | `:5` |
| `shared_path slots=1455 rows=291 path=opendata/data/domains.yaml` | `:6` |
| `shared_path slots=1455 rows=291 path=opendata/data/providers/catalog.py` | `:7` |
| `tree_wide_paths(rows=291): count=4`（就是上面四棵：fixtures / probe_runner / domains.yaml / catalog.py） | `:14` |
| `target_slot_positions: {'contract_pin_2': 1425, 'contract_pin_3': 30}` | `:15` |
| `A_clean: claims=1455 stale_slots=0 stale_rows=0 PASS=True` | `:17` |
| `D_rearm: claim=alpha_vantage::EquityHistorical:contract rewrote=1 stale=True issues=['contract_pin_2_current_hash_drift'] PASS=True` | `:19` |
| `B_tree: stale_rows=291 expected_rows=291 stale_slots=1455 expected_slots=1455 PASS=True` | `:20` |
| `C_specific: stale_rows=8 expected_rows=8 stale_slots=40 tree_rows=291 PASS=True` | `:21` |
| `blast_radius_face arms=4 arms_passed=4 arms_failed=0 PASS=True` | `:23` |

四臂各自的作用，防止"零是个哑计数器"：

* **A** 干净走一遍：`stale=0`，且 claim 数恰为 `rows × 5`（两条独立派生的 291 必须相等，不接受我手打的常量）。
* **D** 单槽 re-arm：把一条 claim 的 `domains.yaml` 槽的 `before/after` 同时换成模拟新摘要，判定器必须报 `contract_pin_2_current_hash_drift`——A 的 0 因此不是接线缺失。
* **B** 全树模拟：`291 行 / 1455 槽`全部转红，且产出的 issue 名集合**恰等于**由槽位普查派生出来的集合（不多不少），这既证明"整树红"，又证明"同一 claim 里别的槽没被牵连"。
* **C** 特异性对照：换成 `opendata/data/models/tmx.py`（本 provider 自己的契约模块，40 槽 / 8 行，位槽 `contract_pin_1`）时只有 8 行红。若 C 也报整树，B 的 291 就没有意义。

`contract_pin_3` 那 30 槽 = 6 行 × 5 phase，逐名可查：`ecb::BalanceOfPayments`、`ecb::YieldCurve`、`fred::NonFarmPayrolls`、`fred::PersonalConsumptionExpenditures`、`intrinio::EtfPricePerformance`、`intrinio::MarketSnapshots`——这 6 行的 `contract` 组多钉了一棵文件，所以 `domains.yaml` 顺移到第 3 槽。

模拟字节 = 现文件 + 一个候选键块（`tmx_etf_historical`，按本仓同族键的字段形状写），落盘为
`artifacts/domains_yaml_after_candidate_block.yaml`，其摘要与仪器内部用的 `sim_target` 同为 `6572339b25d8dc4c…`；`candidate_block_parses=True`、`domains_total=339`（现 338 + 1）。
**边界声明**：块贴在文件尾只是"能解析的追加位置"的估计，真实编辑的插入点不同会得到另一个摘要——但半径与摘要无关，`291/291` 只取决于"字节变了"。

## 4. 面 2：tmx 剩余 16 行的两轴（`runs/tmx_residue_transport_map.txt`，7/7 臂绿，exit=0）

先说这一台仪器为什么长成这样：**这是 clean-room 实现，上游的传输方式不是我们的约束**。第一版只按上游主机分类，被它自己的校准臂当场抓住（见 §7.2）。仓内实测事实：

* `our_client hosts=['app-money.tmx.com'] methods=['graphql','list_rows']`，`witness_seam host=app-money.tmx.com attr=get_shared_http_client host_in_client=True`
* `arm2_ours: landed_fetcher_on_graphql=8 of_accepted=8 residue_with_fetcher=0 of_residue=16` —— 已落地 8 行的 Fetcher 全部且仅调 `client.graphql`；剩余 16 行在本仓**没有任何 Fetcher 类**。
* `arm7_clean_room_divergence: landed_rows_upstream_not_on_seam_host=['EquitySearch', 'IndexConstituents'] count=2 of_accepted=8` —— 这两行上游分别抓 `www.tsx.com` 与 `tmxinfoservices.com`，我们仍走 GraphQL seam 落地了它们。

轴二（域键）：`arm5_domain_key: landed_without_key=[] residue_with_key=[] residue_without_key=16 of=16`。
谓词 `tmx_<snake(model)>` 在 8 条已落地行上全部命中（`tmx_equity_quote` 等 8 键，`domain_keys total=338 provider_keys=8`），在 16 条剩余行上全部不命中；同族可复用键不存在——`EtfHistorical` 有 7 个兄弟键（`alpha_vantage_/cboe_/fmp_/intrinio_/tiingo_/tradier_/yfinance_etf_historical`），其 `contract` 各自是本 provider 的类名，没有一个是可借用的通用键。

| 剩余 16 行 | 域键 | 上游取数主机 |
| --- | --- | --- |
| `CalendarEarnings`、`CompanyNews`、`EtfHistorical`、`EquityGainers`、`IndexSnapshots`、`InsiderTrading`（6 行） | 需新增 | 含 `app-money.tmx.com` |
| `AvailableIndices`、`BondPrices`、`EtfSearch`、`EtfHoldings`、`EtfSectors`、`EtfCountries`、`EtfInfo`、`IndexSectors`、`OptionsChains`、`TreasuryPrices`（10 行） | 需新增 | 仅他源：`bondtradedata.iiroc.ca` / `dgr53wu9i7rmp.cloudfront.net` / `money.tmx.com` / `tmxinfoservices.com` / `www.m-x.ca` |

`arm3_client: single_host_client=True`、`arm6_resolution: unmapped=[] unresolved=[] no_hosts=[] no_roots=[]`、`arm4_upstream_source: via_seam_host=6 via_other_host_only=10 of=16`。
**读法警告**：下表的上游主机是设计输入（数据从哪儿来），不是 blocker 名单——本仓要接哪棵主机由我们的 fetcher 决定；真正的 blocker 只有域键这一轴，因为它落在共享前提文件上。

## 5. 面 3：一次全树 re-sign 的实测价（`runs/producer_scratch_timing.txt`）

生产者按 provider 原子运行（`--output` + `--evidence-root` 指到 bundle 内 `scratch/`，跑完即删；`--evidence-root` 必须在仓内，见 §7.3）。四把实测：

| shard | inventory 行 | legs | PASS/BLOCKED | 墙钟 |
| --- | --- | --- | --- | --- |
| multpl | 1 | 5 | 5/0 | 8 s |
| tradingeconomics | 1 | 5 | 5/0 | 8 s |
| finra | 2 | 10 | 10/0 | 13 s |
| tmx | 24 | 120 | 40/80 | 46 s |

`shards_timed=4 producer_exits_zero=4 producer_timing_exit=0`；四把的 stdout 都回显 `pinned identity set 5168e1e7…`，即重签动的是 pin，不动身份集。
按 `1750 legs / 350 行`的全树规模与 tmx 的 `120 legs = 46 s` 外推，生产者一趟量级在 **10–15 分钟**；**未实测**的是 recorder 那一段（27 次 `--provider` 调用、291 行再签），所以这只是下界参考，不写进任何验收面。
另有 `runs/accepted_rows_per_shard.txt`：`shard_files=27 providers_with_accepted=27 accepted_total=291`，最大 shard 是 `fmp accepted_rows=69`（未计时，上界未知）。

**窗口是红的**：从改 `domains.yaml` 到 27 个 shard 全部重签完成之前，判定器对全部 291 行报 `*_current_hash_drift`，gate member 2 随之红。所以这一步不能拆成"先改文件、下波再补签"，必须一口气做完再提交。

## 6. 非写入见证（`runs/live_tree_untouched.txt` 与提交时刻的 `runs/live_tree_untouched.at_commit.txt`）

两台只读仪器跑前跑后各取一次摘要：

```
b8effc33a88d9e84…  docs/evidence/C101/claims.merged.json
044f3d6493542f23…  opendata/data/domains.yaml
76de40c07a612577…  opendata/data/models/tmx.py
digest_lines=3
live_tree_unchanged=True
tracked_modifications_outside_evidence=0
```

四把 scratch 生产者跑完后 `claims.merged.json` 与 `domains.yaml` 摘要仍为同一对（`runs/producer_scratch_timing.txt` 尾部），`scratch_leftovers=0`。

提交时刻又取了第二代（`runs/live_tree_untouched.at_commit.txt`，`live_tree_untouched_at_commit_exit=0`）：七棵承重前提（`domains.yaml`、`providers/catalog.py`、`models/tmx.py`、`claims.merged.json`、判定器、生产者、tmx 见证模块）逐档与 HEAD 的 blob 字节相同（`shared_premises=7 all_head_eq_worktree=True`，载档 `:13`）。这一代同时修掉了我自己第一把取法的一个缺陷：它先把 porcelain 行切掉前三列、再去判该行是否以 `?? ` 开头——标记已经被切掉，于是未跟踪项被算成"已跟踪修改"，两个计数器恒等（各自打印 6 与 6），那条 0 其实是构造出来的。 shipped 版本保留 XY 状态码并打印状态码普查（`:14` 的 `porcelain_codes={'A ': 23, '??': 277}`），因此 `tracked_modifications_outside_evidence=0`（`:16`）是一个读数而不是恒等式；全树**没有**已跟踪文件的改动，`??` 的 277 条是既存的未跟踪证据收据与本包尚未入库的载档（本包当时未跟踪 1 档，即 `take6`，`:18`；`docs/evidence` 之外未跟踪 6 档全部与本步无关，逐档点名在 `:17`）。身份集与平面在这一代里一并复核：`identity_count=350`、`identity_set_sha256=5168e1e7…e21e65`（`:21`）。
顶部那句 `291/350` 也不是从 HEAD 提交说明转录的：判定器原样再跑一次，`--output` 指到仓外 `/tmp/w31s6_judge/face.json`（不落在任何被 pin 的目录里），stdout 输出行是 `wrote /tmp/w31s6_judge/face.json and /tmp/w31s6_judge/face.md: 350 identities, 291 offline accepted`、`exit=0`。这条跑测不落进本包（理由同 §10 里 dir 模式那一条：落进来就又是新的一层未覆盖档），命令原样写在这里，任何读者可以同样复算。

## 7. 仪器缺陷与红面（留在盘上，不覆盖）

1. `runs/domains_pin_blast_radius.take1_slot_position_red.txt`（`arms=4 arms_passed=3`，exit=1）：B 臂把"应出现的 issue 名"取自**第一条** claim 的槽位，于是 `contract_pin_3_current_hash_drift`（6 行 × 5 phase）被判为多余。这是我自己仪器的缺陷，不是数据变了——改正方式是先做槽位普查、再要求 issue 集合**等于普查派生集**，改正后半径结论不变（仍是 291/1455）。
2. `runs/tmx_residue_transport_map.take1_upstream_hosts.txt`（内含 `Traceback`，`TypeError: expected string or bytes-like object, got 'set'`，exit=1）与 `runs/tmx_residue_transport_map.take2_arms6_green.txt`：**上一代的红面已经不存在了**。按上游主机分类的那一版 arm2 读到 `landed_served_on_seam=6 of_accepted=8`，我随后重写了仪器并 `cp` 归档，而 `cp` 发生在重写之后，于是这个"6/8"的原始 stdout 被我自己的输出覆盖、没有存活载档。因此本面把该读数标为**不可复算**，并在下一代的 arm7 里用文件重新导出同一个数（`count=2`、点名 `EquitySearch`/`IndexConstituents`），此后它每次跑都会重算。另附同类缺陷一条：`_closure` 曾把"主机集合"当"函数体字符串"喂给 `re.findall`。
3. `--evidence-root` 指向仓外 `/tmp` 时，生产者在
   [iter2_claims_producer.py:76](../../../../scripts/quality/iter2_claims_producer.py#L76)
   抛 `ValueError: '/tmp/…' is not in the subpath of '/Users/…/opendata'`（两把都 `producer_exit=1`，`elapsed_seconds=0`）。这两把 stdout 写在 `/tmp` 且已被删除，同样标为**不可复算**；持续有效的是那行源码位置，以及"scratch 证据根必须落在仓内"这条使用前提。
4. `runs/staged_payload_credential_scan.take1_suppressed_counter_none.txt`：第三台仪器（§10）第一代把"allowlist 抑制差值"写成了
   `if "B_allowlist_stripped" in readings:`——`readings` 是**字符串列表**，`in` 做的是成员判断而不是子串判断，条件恒假，于是那一行打印成
   `allowlist_suppressed_in_this_payload=None`。11 臂全绿、exit=0，一个"看起来是读数、其实是没读"的值照样混过了判定面。这正是我记录过的那类缺陷（静默默认值伪造计数），修法是把每臂 baseline 存进 `baseline_counts` 再相减，改正后实测 `=0`（`runs/staged_payload_credential_scan.take2.txt`）。
5. `runs/a2_static_plane_bandit_red.txt`（`FAIL: A2 gate failed on: immutable evidence secret scan, bandit`，末行 `a2_static_plane_bandit_red_exit=1`）：三台仪器自身是 A2 平面的输入（`scripts/quality/a2_check.py:120`），而 `probes/staged_payload_credential_scan.py` 一把就带三项 bandit 命中（`B404`/`B108`/`B607`）。缺陷性质不是"扫描器太严"，是我按 ruff 写了 `# noqa: S603/S108/S607` 就以为抑制做完了，而 bandit 读的是**另一套**行内标记（`# nosec B<id>`）——两套抑制互不相认，于是我自己的 ruff 全绿掩盖了 bandit 全红。修法按 [bandit.yaml:20-24](../../../../bandit.yaml#L20) 的 C81 家规逐行补 `# nosec`（不引入全局 `skips:`，那等于对整棵树闭嘴、包括没人看过的代码）；改后终轮四面 `ok`（`runs/a2_static_plane_final.txt:67-69`、`:85`）。同族更早一条：清理前的 tmx 仪器有 33 项 ruff 报错（28 项 E501 等），该中间态没有载档，已在 §11 末标为不可复算。

## 8. 载档清点

以磁盘为准（`glob` 实测，清点发生在下面这段写完之前）：`probes/` 3 把 .py、`runs/` **20** 把 txt、`artifacts/` 1 把 yaml、加本 `face.md` = 提交载荷 **25** 档；`scratch/` 0 个文件（四把计时跑完即删；空目录本身不进 git，`.gitignore` 也不覆盖它）。`probes/__pycache__/` 被 `.gitignore:2` 覆盖，不入库。
`runs/` 20 把的构成（逐档点名，2+3+6+6+3=20）：面 1（钉死半径）2 把——`domains_pin_blast_radius.txt`（终轮 4/4 臂）与 `domains_pin_blast_radius.take1_slot_position_red.txt`；面 2（tmx 两轴）3 把——`tmx_residue_transport_map.txt`、`…take1_upstream_hosts.txt`（含 Traceback 的红面）、`…take2_arms6_green.txt`；前置普查与非写入见证 6 把——`accepted_rows_per_shard.txt`、`live_digests_before.txt`、`live_digests_after.txt`、`live_tree_untouched.txt`、`live_tree_untouched.at_commit.txt`（§6 的提交时刻第二代）、`producer_scratch_timing.txt`；凭证扫描 6 代——`staged_payload_credential_scan.take1_suppressed_counter_none.txt`、`…take2.txt`、`…take3_payload17_no_digest_arm.txt`、`…take4_final_payload.txt`、`…take5_cover_final_index.txt`、`…take6_final_index.txt`；A2 静态平面 3 把——`a2_static_plane_bandit_red.txt`、`a2_static_plane_final.txt`、`a2_static_plane_control_untouched_probe.txt`。
代际更正留痕（四代都留数，不静默覆盖）：本节第一代写"runs/ 11 把"，第二代写"15 把"，第三代写"17 把（`take5` 前）/ 18 把（`take5` 后）"，都是 prose 侧的 off-by-one 与漏算；`18 → 20` 走的两档分别是 §10 的最后一代扫描 `take6` 与 §6 的提交时刻非写入第二代。此后本节不再自数：`runs/` 的档数一律由下面的 glob 读数给出。
"每档自带读法"这条也不是叙述：对 `runs/` 逐档跑 `\b\w*_(exit|rc)\s*=\s*-?\d+`，命中 0 把缺失（`no_exit_line=[]`、`count=0`，跑在最终 20 把上），即没有任何载档需要由本节替它签出口读数。`take6` 的出口行是 `staged_payload_credential_scan_exit=0`（载档 `:28`），提交时刻非写入代的出口行是 `live_tree_untouched_at_commit_exit=0`（载档 `:23`）。

## 9. 交给 ledger owner 的选择，以及选完之后该看到什么

四个可选项（本步只测不选）：

* **A 为一行付全树重签**：一次 `domains.yaml` 追加 + 27 shard 重生产 + 291 行再签，换来 plane 1 `291 → 292`。
* **B 一批摊薄一次重签**：先在同一批里凑齐"要动 `domains.yaml`/`catalog.py`/`fixtures`/`probe_runner` 的行"再付一次。tmx 这 16 行全部落在同一棵文件上，是天然的批次；但**其余桶是否也落在这四棵全树文件上，本步未测**（§4 的警告同理），所以现在还不能断言 B 比 A 便宜多少。
* **C 摘掉 `domains.yaml` 的 contract 钉**：等于放宽判定器，会把"证据指向的字节确实存在"这条保证换成口头保证。不在我的处置权内，列出仅为完整。
* **D 暂不动共享前提**：转去做只碰 provider 本地文件的桶；代价是平面 1 停在 291。

预测判据（选了 A 或 B 之后，下一面必须读到）：plane 1 `= 291 + K`（K = 本批新行数）；面 1 的 A 臂对 `291 + K` 行读到 `stale=0`；`domains_total` 比本面的 339 基线恰好多 K… **不**——`domains_total` 只随**键数**增长，与行数不是一比一时该读数就按键数走，二者的差要显式列出；身份摘要仍为 `5168e1e723999312…`。
失效条件：若重签后 A 臂仍读到 `stale>0`，说明有 shard 没重签，issue 名里的 `{group}_pin_{i}` 直接指出是哪个槽；此时不得翻转状态，应把该 shard 补签。
读数口径：本面所有 291/1455/20744/589 的分母都是"已验收行的 claim 与其 pin 槽"，不是"全部 1565 条 claim"，两者差在 `INCOMPLETE/UNREVIEWED` 的 claim 上。

## 10. 提交前凭证扫描（第三台仪器 `probes/staged_payload_credential_scan.py`，13/13 臂绿，exit=0）

为什么是仪器而不是一条 `gitleaks` 命令：扫的对象必须是**索引里的 staged blob**（逐档 `git show :path` 重建到 `/tmp/w31s6_scan/payload`），不是工作树。工作树里可能有没 `git add` 的改动，而这次提交真正携带的字节只在 index 里。

| 读数 | 载档行（`runs/staged_payload_credential_scan.take4_final_payload.txt`） |
| --- | --- |
| `staged_files=18 staged_outside_bundle=0 outside_list=[]` | `:1` |
| `payload_materialized_from_index_files=18` | `:2` |
| `payload_index_sha256=0cde06135462b426…26cad82`（把"扫的是哪些字节"绑死，不只代表扫了 N 档） | `:3` |
| `worktree_differs_from_index_files=0 list=[]` | `:4` |
| `A_shipped: baseline=0 planted=2 planted_rules_delta={'github-pat': 1, 'generic-api-key': 1} moved=True` | `:5` |
| `B_allowlist_stripped: baseline=0 planted=2 … moved=True` | `:6` |
| `C_builtin_no_config: baseline=0 planted=2 … moved=True` | `:7` |
| `allowlist_suppressed_in_this_payload=0 threshold=none` | `:8` |
| `plant_files_inside_staged_payload=0 names=[]` | `:9` |
| `gitleaks_*_exit=0`（6 次扫描逐次自带） | `:10` |
| `credential_scan_face staged_files=18 gitleaks_runs=6 arms=13 arms_passed=13 PASS=True` | `:11` |

三棵配置 × 两态（干净 / 植入）= 6 次扫描。植入的是一枚运行时拼出来的假 token（源码里没有完整字面量），报告只解析条数与 `RuleID`，`Match`/`Secret` 字段从不进 stdout，所以载档里不含该字面量。

* **A**（shipped `.gitleaks.toml`）baseline=0 是提交前置条件；A 在植入后必须 >0，否则那个 0 与"扫描器根本没跑"不可区分。
  这个 0 不是靠路径豁免换来的：shipped 配置自己写着设计规则
  [.gitleaks.toml:3-4](../../../../.gitleaks.toml#L3)「do not exclude paths from scanning. A wide `^docs/` allowlist previously hid a real token literal in docs/evidence/」，
  其 allowlist 条目是按 secret 形状（Fed 序列标识、`"<path>": "<64hex>"` 摘要对）豁免、每条都带自己的 counterfact 载档，不是按目录豁免；B 臂把这些条目全剥掉后 baseline 仍是 0，才是"本包 19 档里没有任何命中被掩盖"的正面读法。
* **B/C** 的 baseline 相等（臂 `config_strip_cross_check`）证明我剥掉的只有 allowlist，没有连带剥掉规则。
* `worktree_differs_from_index_files=0`（臂 `index_is_worktree`）：每个 staged 路径的工作树字节与 index 字节逐档相等。没有它，"扫过 index"与"提交里其实是另一份字节"可以并存。
* `allowlist_suppressed=0` 的口径边界：说的是"这 18 个文件里没有任何命中被 shipped allowlist 掩盖过"，不是"本仓别的提交也不会被掩盖"。顺带一次实测：我拿本仓那条例径最大的形状规则做过对照物——伪造一条 `"docs/…/x.py": "<64hex>"`，三棵配置**都**是 0 命中，因此它不能充当差分对照（对照臂必须能为非零）；本仪器的 re-arm 只用能动的 token 植入。
* `plant_files_inside_staged_payload=0`：植入件只存在于 scratch 树的副本，staged payload 里没有它的文件名，防止"控制用的假密钥"变成入库证据。

自覆盖的收口（这一条不能靠叙述签字）：任何扫描 stdout 都不可能扫到生成它的那一次运行，所以扫到第 N 代时第 N 代自己永远是唯一漏网档。代际实测：take1/take2 各覆盖 15 档 index（14 档 bundle + 本仪器源码），take3 覆盖 17 档，take4 覆盖 18 档（新增的正是 take1/take2/take3 三把载档）。第 5 代 `take5`（`runs/staged_payload_credential_scan.take5_cover_final_index.txt`；写下这段时当作最后一代，本节末尾的第 6 代把它取代为"倒数第二代"）扫提交前的最终 index，预测判据：`staged_files=22`、三臂 `baseline=0`、`arms_passed=13`、`worktree_differs_from_index_files=0`；失效条件：任一 baseline 非 0，或 `staged_files != 22`（说明新增载档带进了命中，或 index 与工作树不一致）——那会作废本节"可提交"的结论，必须先清除再重跑。
`take5` 已跑，四条判据全部命中。载档第 4-8 行是"预测 vs 实测"的并排块（写预测在前、跑测在后），逐条读数取自行号本身：`staged_files=22`（`:13`）、三臂 `baseline=0 0 0`（`:17-19`）、`worktree_differs_from_index_files=0 list=[]`（`:16`）、`arms=13 arms_passed=13 arms_failed=0 failed=[] PASS=True`（`:23`），另附 `payload_index_sha256=69b93aa0c861a5f5…ad07a61`（`:15`）与自读出口 `staged_payload_credential_scan_exit=0`（`:24`）。
植入臂照旧能动：三棵配置都从 0 走到 2（`github-pat` + `generic-api-key`），所以那一串 0 不是"扫描器没跑"。
载档本身也被验过一次：写完后回读整档 grep 植入 token 的前缀，命中 `False`，即"控制用的假密钥"没有混进入库证据。
代际改判留痕（两次都留数，不静默覆盖）：第一代把 take5 的判据写成 `staged_files=19`，那是"19 档已 staged、A2 平面还没有载档"时的推导；§11 落第一、二把载档后我改成 `19 + 2 = 21`，这又错了——A2 平面实际落了三把（红 take、绿 take、对照臂），漏算的正是红 take 那一把，`git diff --cached --name-only | wc -l` 实测 22。所以判据第二次改到 22，`19` 与 `21` 都是被取代的推导值而不是笔误：这连续两次都是同一类缺陷（prose 侧自己数档，而 index 只有 `git` 能数），第三次起该读数一律从 `git diff --cached --name-only -z` 直接取，本仪器的 `staged_files` 就是这个口径。
`gitleaks` 版本 8.30.1，`/opt/homebrew/bin/gitleaks`；`--exit-code 0` 让 rc 只做"运行是否成功"的通道，命中数一律从 JSON 报告条数读出，不从 rc 反推。

第 6 代（`take6`，写在跑测之前，跑测之后不改这段的预测部分）：take5 落盘后我又改了一次本节（把 `:9-12` 的行号引用改成逐行引用、按 bandit 载档 JSON 的 `line_number` 重签 §11 两行、把出口读数普查从 17 把重跑到最终 18 把），所以 take5 扫的那份 `face.md` 字节已经不是将要提交的那份。`git add` 之后从 `git diff --cached --name-only -z` 直接取到的口径实测 `staged_count=23`（构成：1 md + 3 py + 18 txt + 1 yaml），`outside_bundle=0`。预测判据：`staged_files=23`、三臂 `baseline=0 0 0`、`arms_passed=13`、`worktree_differs_from_index_files=0`、`plant_files_inside_staged_payload=0`。失效条件：任一 baseline 非 0，或 `staged_files != 23`，或 `worktree_differs_from_index_files != 0`（index 与工作树不一致 = 提交会带走扫描没见过的字节）。
take6 自己与其后的实测段仍在这一层之外——那就是本节已经写下的结构上限，处理办法不是再造第 7 代（第 7 代只是把漏网档往后挪一位），而是把最后一层交给两个能为非零的检验：① 回读 `take6` 与本节改后字节，grep 植入 token 前缀（该前缀只在运行时拼装，不来自任何字面量）；② 用 shipped 配置对**本包目录整体**跑一次 `gitleaks --no-git` 的 dir 模式扫描——它读的是工作树字节而不是 index，所以它扫得到的正是 index 扫描扫不到的那两档（take6 载档本身 + 它之后的 prose）。dir 模式的命中数在本回合的跑测输出里打印；任何读者可用同一条命令复算，因此它不是叙述性断言。

`take6` 已跑，五条判据全部命中，读数按载档行号给出（`runs/staged_payload_credential_scan.take6_final_index.txt`）：`staged_files=23 staged_outside_bundle=0 outside_list=[]`（`:17`）、`payload_materialized_from_index_files=23`（`:18`）、`worktree_differs_from_index_files=0 list=[]`（`:20`）、三臂 `baseline=0 0 0` 且三臂 `planted=2 moved=True`（`:21-23`，规则增量 `github-pat` + `generic-api-key`）、`plant_files_inside_staged_payload=0 names=[]`（`:25`）、`allowlist_suppressed_in_this_payload=0`（`:24`）、`arms=13 arms_passed=13 arms_failed=0 failed=[] PASS=True`（`:27`）、`staged_payload_credential_scan_exit=0`（`:28`）；`payload_index_sha256=2945de62bc7b2660…b39e5d5`（`:19`）。这一代取代 take5 的理由写在载档头部（`:9-15`）：take5 扫的 `face.md` 字节不是提交带走的那份。
最后两层的实测闭合同时兑现了上面两个检验：dir 模式扫本包目录（shipped 配置，`--no-git`，扫的是工作树副本，植入件只写进 `/tmp` 的副本）**clean=0 / planted=2 / control_moved=True**——对照臂能为非零，所以那个 0 不是"扫描器没跑"；回读 `take5`（2181 字节）与 `take6`（2712 字节）两把载档与 `face.md`，植入 token 前缀命中 `False`，这两条 grep 读数落进了 §6 的提交时刻载档（`:19-20`），植入前缀本身不入库。dir 模式的那一次跑测不落进本包（落进去就又是新的一层未覆盖档），它的命令与输出形态就是上一段那句"任何读者可用同一条命令复算"。
delta 的口径要说清：`take6` 扫的是当时的 23 档 index，而这次提交带走 **25** 档——多出的是 `take6` 载档本身与 §6 的提交时刻非写入代，两者都是本步自己的读数而不是新的输入面。补齐后的 dir 模式跑测把这 25 档全部扫过（`gitleaks detect --source <本包目录> --config .gitleaks.toml --no-git`，报告条数从 JSON 读，不从 rc 反推），`clean=0 / planted=2`；这条 dir 臂在最后一次文字改动之后又跑了一遍，两次读数相同（`take6` 载档两次都打印 2712 字节）；`take5` 载档的 2181 字节由 §6 的提交时刻代打印一次，两把载档的植入前缀 grep 都是 `False`。

## 11. 三台仪器自身的 A2 静态平面（红 → 绿 → 对照）

为什么本步要专门跑这一面：`scripts/quality/a2_check.py:120` 的 `_is_a2_candidate` 把**任何改动的 `.py`**（除首段目录是 `akshare`/`frontend`、除 PORTED 树）送进 ruff 全量规则 + `ruff format --check` + mypy + bandit。`docs/evidence/**/probes/*.py` 不在排除之列，所以本包三台仪器与产品代码同标准——证据面的代码不是二等公民。

| 读数 | 载档行 |
| --- | --- |
| 红 take（清理后、逐行豁免前）：`ok ruff check` / `ok ruff format --check` / `ok mypy (3 unique target(s))` / `FAIL bandit` | `runs/a2_static_plane_bandit_red.txt:66-68`、`:82`、`:84` |
| bandit 三项 `B404`/`B108`/`B607`，全在 `probes/staged_payload_credential_scan.py` 一把档里；载档 JSON 的 `line_number` 是红 take 当时的源码行（41 / 52 / 78，见同档 `:155,160`、`:175,180`、`:195,205`），补完 `# nosec` 后同三处在当前源码是 `:41`、`:53`、`:79`——`#:` 注释上移了一行 | 红 take `:160/:180/:205` |
| 修法依家规：[bandit.yaml:20-24](../../../../bandit.yaml#L20)（C81）禁止全局 `skips:`，每条豁免须在**自己那一行**写成 `# nosec B<id>  # <reason>`；先例 `docs/evidence/C104/h15-series-enumeration/probes/payload_rescan.py:15,23,35` | 源码内 |
| 绿 take（同一条命令、同样三档）：四面 `ok`，唯一 FAIL 是 `pinned Gitleaks file scan` | `runs/a2_static_plane_final.txt:67-69`、`:85`、`:83` |
| 该残留红的**对照臂**：同一条 `a2_check --files`，喂一个本包完全没碰过的已入库档 `payload_rescan.py`，四面 `ok`、同一平面同一红（`returned 1` 共 25 行，载档 `:14-62` 的奇数行；出口读数写在头部 `:4`；`FAIL:` 在 `:7`） | `runs/a2_static_plane_control_untouched_probe.txt:64-66`、`:72`、`:14-62`、`:7` |

对照臂是这一节的关键，没有它"25 档与我无关"只是我说的一句话。同一台门、同一切片机制，输入换成一个未改动的文件，`pinned Gitleaks file scan` 仍读到同样的 25 档，且 25 档全部落在 `docs/evidence/C100/**/inputs/pristine/akshare/`（封存的上游 vendored 原文），`C135` 名下 0 档——两把载档里的档数与路径集合我都重数过（绿 take 与对照臂各 25、集合相同）。也就是说这条红**先于本包存在**，与"本包引入什么"无关；我不据此声明门已全绿，只据此把"本包引入的红"与"承重的既有红"分开口径。既有记录同向：`docs/evidence/C100/**` 的原始收据自 C113 起被 `.gitignore` 覆盖，本包装不了它。

清理不改测量的见证（这条必须有，否则"修格式"与"修出不同结果"不可区分）：A2 化之后两台量测仪器各重跑，stdout 与**已入库载档**逐字节相同——`diff runs/tmx_residue_transport_map.txt <重跑>` 与 `diff runs/domains_pin_blast_radius.txt <重跑>` 都无输出、`diff` exit 0。所以改的只有字节排版与注释，7/7 臂与 4/4 臂的每个读数原样保留。

同一次直跑的两条不变量（走仓内判定器，不手抄语义）：`judge.load_inventory()` 实测 `identity_count=350`、`identity_set_sha256=5168e1e723999312b0978bf9b1a8999c0b4960cdb12013e43e1e59e349e21e65`，与判定器里钉死的 `EXPECTED_IDENTITY_SHA256` 相等（该函数在不等时直接 raise，所以任何 `exit=0` 的仪器跑本身就携带这条见证）；`judge.build_report()` 实测 `offline_acceptance_count=291`、`status_counts={'INCOMPLETE': 22, 'OFFLINE_ACCEPTED': 291, 'UNREVIEWED': 37}`，与 §3 的分母同代。

一条**声明为不可复算**的读数：把 tmx 仪器从 33 项 ruff 报错（28 项 E501 + B009/PERF401/D103/C416 各若干）修到 0 的那一步发生在首跑 `a2_check` 之前，中间态没落载档，因此本面不把它写成读数。可复算的只有改后的 0 项，即上表首行的 `ok ruff check`。
