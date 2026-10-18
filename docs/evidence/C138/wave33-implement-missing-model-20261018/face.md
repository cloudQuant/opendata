# C138 wave-33 收口面：implement_missing_model 落地集——re-sign 的价格 0/317 STALE、两个 mover、和一把还能咬人的 GraphQL allow-list

一句话：平面 1 从 **317 涨到 332 / 350**（claim 数 1695 不动，`INCOMPLETE` 22 → 7，`UNREVIEWED` 11 不动），残差经出货 census **33 → 18**；全部移动来自 15 行新落地——tmx `8 → 22`（4 个 GraphQL operation + ETF loader 家族，`_OPERATIONS` 7 → 11）与 `government_us::TreasuryPrices` `5 → 6`，代价是 `domains.yaml` 被每一张已验收 receipt 的 contract pin 钉住，所以本波重签了 **317 行**，价格是被 `A_pre_chain_judged` 量出来的，不是叙述出来的。

## 1. 仪器与判定源（判定不手抄）

* 判定器：`scripts/quality/iter2_model_evidence.py`，本波收口探针 `probes/w33_landing_radius.py` 进程内 import 它跑两代判定（HEAD 的 `claims.merged.json` 拷到 `/tmp/w33_landing_radius/claims.head.json` 对今天的树、工作树对今天的树），`--output` 只落 /tmp，任何已钉档都不写。
* 形状与纪律抄自 C137 的 `w32_landing_radius.py`：同代载档、每臂能红、载档按名走全集并排除自己。
* census：C128 的 `docs/evidence/C128/plane1-residue-readiness-20261017/probes/residue_axis_census.py` 原样重跑（字节未动），stale 谓词用 C135 出货的 `_walk_claims`。
* 反例腿：出货 `scripts/quality/iter2_claims_probe_runner.py --phase counterfact`，`--evidence-root` 恒为 `/tmp/w33_landing_radius/evidence_root`（scratch）。
* 载档：本面全部读数出自 `runs/w33_landing_radius.txt`（下列 `:N` 即该档行号，`grep -n` 于本探针最后一次、lint 之后的运行；探针字节此后未动）。

## 2. 面 1：re-sign 的价格与漂移半径

| 读数 | 载档行 |
| --- | --- |
| `A0_pin_table HEAD claims pin domains.yaml on 317/1695 rows, 1585 byte-pin slots` | `:4` |
| `A_pre_chain_judged accepted=0/350 statuses={'STALE': 317, 'UNREVIEWED': 11, 'INCOMPLETE': 22} claims=1695 identity_set=5168e1e723999312 judge_exit=0` | `:6` |
| `B_post_chain accepted=332/350 statuses={'OFFLINE_ACCEPTED': 332, 'UNREVIEWED': 11, 'INCOMPLETE': 7} claims=1695` | `:8` |
| `B0_coverage providers_with_accepted=28/32 zero_accepted=['biztoc','seeking_alpha','stockgrid','wsj']` | `:9` |
| `C_drift head stale_rows=317 disk stale_rows=0 forged_digest stale_rows=332/1695` | `:10` |

wave 32 的价格是 291 STALE（harness 被每张 receipt 钉为 `probe`）；wave 33 是 **0 accepted / 317 STALE**，因为本波动的是 `domains.yaml`，它被 317/317 已验收行钉着（1585 个 pin 槽）。`C_drift` 是对这个谓词的反例臂：伪造一个 digest，332 行全部读出漂移——探测器仍然咬人；工作树侧 stale=0 说明链把价格付清了。

## 3. 面 2：谁动了（归因），以及这个归因臂怎么被证明能为红

| 读数 | 载档行 |
| --- | --- |
| `D_recomputed_trap accepted 0 -> 332 movers=28` | `:11` |
| `E_landed_report accepted 317 -> 332 movers=['government_us','tmx'] deltas={'government_us': 1, 'tmx': 14} regressions=[] tmx 8->22 government_us 5->6` | `:12` |
| `F_control_forged row=alpha_vantage::EquityHistorical … regressions=['alpha_vantage:3->2'] accepted 332 -> 331 face_bites=True` | `:13` |
| `F_recheck_real … movers=['government_us','tmx'] regressions=[] identical_to_E=True` | `:14` |

D 是 wave 32 §5 记录的陷阱的本波读数：**重算后的 before 读 0 accepted，28 个 provider 全成"mover"**，所以它只被打印来定价，不做归因。真归因比的是 HEAD 已提交的判定报告 `docs/evidence/C101/judge/claims.merged.report.json`（317 landed：tmx 8、government_us 5），mover 集合恰为 {tmx +14, government_us +1}、零回归。F 用内存里翻掉一行证明 `E` 能为红（`face_bites=True`），翻完复算与 E 逐 provider 相等。

## 4. 面 3：census 翻转与它自己那把红臂的归因

* `G_census_rerun exit=1 … residue=18 actions={'implement_missing_model': 4, 'ledger_ruled_out': 11, 'new_provider_package': 2, 'shard_entry_needs_harness': 1} arms=8 arms_passed=7 arms_failed=1`（`:15`）；出货行动表 `sums=18`（`:16`）。残差 33 → **18**，`implement_missing_model` 收缩为 `{tmx: 2, yfinance: 2}`。
* 红臂逐字读数：`G_census_red_arm failing=['B_landed_negative'] cause=yfinance::BalanceSheet landed (accepted=True)`（`:17`）。本波**没有**修它也没消耗别的控制：wave 32 §6.2 已定位根因（census 把 BalanceSheet 写死成负面对照，而该行已落地），重装备在 C137 的 `w32_census_control_rearm.py`，C128 两把探针字节本波未动。本面把它原样报出来，而不是替它辩护。

## 5. 面 4：GraphQL allow-list 长了 4 个，但门还是门

`H_graphql_gate operations 7 -> 11 new=['getCompanyInsidersActivities','getEnhancedEarningsForDate','getNewsAndEvents','getStockListSymbolsWithQuote'] unregistered_live_call=TmxProviderError:TMX_OPERATION_NOT_ALLOWED`（`:18`）；`H_domains_radius keys 338 -> 353 (+15)`（`:19`）。7 → 11 是被测出的**枚举增长**，不是扩洞：探针当场以未注册操作名调 `client.graphql`（`client.py:181` 的查表、`:183` 的 raise），网络动作发生之前就被拒——allow-list 是 `_OPERATIONS.get(...) is None` 的封闭枚举，没有 wildcard 分支可被"顺手放宽"。

## 6. 面 5：拒绝形状——counterfact 重放，不是 AST 判定

wave 32 的教训（AST verdict ≠ reachability）在本波按行兑现：出货 `--phase counterfact` 腿在 scratch root 上重放，打印判定器自己的 verbatim 拒绝与它点名的站点。

| 读数 | 载档行 |
| --- | --- |
| `I_counterfact_leg row=tmx::EtfHistorical exit=0 verdict=VERDICT=PASS refusal=TmxProviderError:TMX_BAD_ROWS site=…fetchers_w33_graphql.py` | `:20` |
| reason 原文（首 220 字符）："the refusal is raised by EtfHistoricalFetcher.transform_data … at the `len(source_rows) > _history_limit(params)` guard -- the site this leg's traceback actually ends in" | `:21` |
| `I_counterfact_leg row=government_us::TreasuryPrices exit=0 … refusal=GovernmentUSProviderError:TREASURY_PRICE_BAD_VALUE site=opendata/data/providers/government_us/models/treasury_prices.py::TreasuryPricesFetcher.transform_data` | `:22` |
| reason 原文："its bid cell reads 99.985500x, which is neither a number nor empty … must refuse rather than coerce" | `:23` |
| `I_leg_roots legs=2 distinct=['/tmp/w33_landing_radius/evidence_root'] c101_root_seen=False` | `:24` |

15 行各自的守卫形状记录在 `notes/per_row_scoping.md` 与本波 witness shard；本面对"每个家族"的重放是 GraphQL 族与 government_us 族各一行，其余族（ETF loader）与 26 行 yfinance 一样由链的 producer 载档给全量相面，不由这把探针冒充。

## 7. 面 6：仪器发现——offline 腿不执行声明的 `allowed_hosts`（本面两边都量了）

* `J_offline_leg_hosts row=government_us::TreasuryPrices kind=shared_http_client declared=['api.fas.usda.gov', …] forged=['sinkhole.invalid.example'] requests=1 rows_served=1 child_exit=0`（`:25`）：把 entry 的 host 表整把换成 sinkhole，seam fake 照发 1 行——**离线相面对 host 授权是盲的**。
* 代码路径：runner 在 `iter2_claims_probe_runner.py:1039` 把 CONFIG 的 `allowed_hosts` 拷进 entry、在 `:140-152` 用它构造 `_grant_context`（`:152`），但 fake carrier 按 `url_contains` 应答，从不消费这份 grant；真请求路径执行它——`opendata/data/request_budget.py:459` `if host not in grant.allowed_hosts:`（`:26` 当场读出该行），授权面另见 `opendata/data/source_policy.py:105`。
* 这不是本波引入的：`rows_w33_treasury_prices.py` 的 docstring 已自报 treasurydirect.gov 不在 parent CONFIG 的 `allowed_hosts`（`iter2_witnesses/government_us.py:311-316`），live 腿落地前需要 host 进 grant——处置归 harness/policy owner，本面只把它记为仪器发现。

## 8. 面 7：载档与链

`K_carriers population=…/runs/*.txt files=61 walked=60 self_carrier_excluded=w33_landing_radius.txt missing_exit_line=[] chain_exit_readings=59 chain_exit_nonzero=[] negative_readings=[] summary_flags={'nonzero_members_empty': True, 'merge_idempotent': True, 'stable_sha_hits': 4} recorders={'recorder_tmx.txt': (22, 2), 'recorder_government_us.txt': (6, 0)}`（`:27`）。链：`probes/w33_run_chain.py`，28 provider producer→recorder→merge×2→judge 共 60 把既有载档，`chain_summary.txt:81` = `nonzero_members=[]`、两 take 稳定载荷 sha `21b59eebf5974133`、`post_chain_accepted=332`。`recorder_tmx.txt:33` `signed=22 refused=2`、`recorder_government_us.txt:15` `signed=6 refused=0`。本探针的载档 `:29` = `w33_landing_radius_exit=0`。

面线：`landing_radius_face arms=11 arms_passed=11 arms_failed=0 failed=[] pre_chain_accepted=0 post_chain_accepted=332 movers={'government_us': 1, 'tmx': 14} census_residue=33->18 PASS=True`（`:28`）。

## 9. 本波没有落什么（7 行，每行给测量，不给 TODO）

分母：残差 18 = 4 `implement_missing_model` + 2 `new_provider_package` + 1 `shard_entry_needs_harness` + 11 `ledger_ruled_out`。以下 scoped-out 行的测量取自 `notes/per_row_scoping.md` §4（已重读）：

* **tmx::OptionsChains**（IN）：transport 实测是 `get_current_options`（`read_html`）+ `download_eod_chains`（`read_csv`）打 `www.m-x.ca`——HTML 表与文件下载，`client_seam` 的 GraphQL envelope witness（`{"data": {root: …}}`）表达不出来。
* **tmx::IndexSnapshots**（IN）：一个模型同时要 `get_data_from_gql` + `get_data_from_url` + `get_indices_backend` + `money.tmx.com/en/quote` 页面——3–4 条腿跨 3 个 host，仓内只存在其中一条。
* **yfinance::AvailableIndices**（IN）：**0 个网络端点**——上游从常量 `INDICES` dict 转置（`utils/references.py:52`）。障碍不是代码是验收面：wave 32 `E_seam_kinds` 把当时全部 317 行收在三种 transport 形状的 seam 里，零腿 carrier 没有先例；派单前先用一行探针定价，不在本波吞。
* **yfinance::FuturesCurve**（IN）：SDK 形状，而该包里每个候选文件半径 **26**（`_sdk.py`/`_session.py`/`provider.py`/witness）——为 1 行重开 wave-32 的 SDK seam = 重签 26 行，本波唯一的全树价就是 317，不叠两个。
* **biztoc::WorldNews / stockgrid::ShortVolume**（NP）：`globbed=0`、包体为空，落地=整 provider + `catalog.py` 树-wide 改动（`_reserved_provider`），`rights_status=NOT_REGISTERED`（biztoc 还要 RapidAPI key）。
* **yfinance::KeyMetrics**（S）：installed yfinance 0.2.66 没有 valuation-measures 面（`H_keymetrics class_has_get_valuation_measures=False`，wave 32 已量）；census 仍把它读成 `shard_entry_needs_harness`——同一把仪器盲点，本波不重复收费也不顺手修。
* `ledger_ruled_out` 11 行不进平面 1。**所以平面 1 当前上界是 332 + 4 + 2 + 1 = 339，不是 350。**

## 10. 撤回与本单未复现的数

1. **`tests/test_authority_reconciliation.py` 的红不是本波挣来的声称**：`…::TestLiveTableMatchesLiveRegistry::test_domains_without_a_row_are_exactly_the_single_source_ones`（`tests/test_authority_reconciliation.py:503`，断言在 `:509`）在 **HEAD 上就是红的**：本仓 detached-HEAD worktree 实测 `unlisted=321`、钉集 `NATIVE_MODEL_DOMAINS=65`（断言比较集含 3 个 `SINGLE_SOURCE_EXEMPTIONS`，即"68"的构成），domains served 总 336。一位代理把钉集 re-base 到 333（+3 = **336** == 工作树实测 unlisted=336；served 351），折进了 **253 个本已存在的名字**加本波的 15 个——工作树现在 `1 passed`。派单说"pinned 68"，实测是 65+3；253 = 321 − 68 成立。**这是关于仓库账本的大主张，wave 33 只贡献了其中 15/271 的移动**，测试文件本面对我read-only，此节按复算登记。
2. **tmx "before 8 accepted + 2 INCOMPLETE" 的 INCOMPLETE 侧未复现**：HEAD 已提交报告里 tmx INCOMPLETE 是 **16**（本波把其中 14 行翻成 accepted 才到 22）；accepted 侧 8 → 22 与 government_us 5 → 6、yfinance 平 26、census 33 → 18、`332 / 350`、`claim_count=1695`、identity `5168e1e723999312`、60 载档、`21b59eebf5974133`、recorder `22/2` 与 `6/0` 全部复现。
3. **探针第一代载档不可引用**：第一次运行在 census 子进程调用处抛 `TypeError`（`_run` 没带 env 形参），carrier 被半程输出覆写过；本面行号全部出自 lint 之后的最后一代，前一代理不承诺任何行号。
4. **归因臂没有用 `provider_labels_match_inventory` 的 wave-32 原形**：本波把 inventory 标签核对挂在 D 臂（`trap["providers"] == expected_providers`，`:11`）而不是每臂重复——引用时别把 D 的 32-provider 核对读成 E 的。

## 11. 口径边界（披露，不外推）

* `E_landed_report` 的 before 是 HEAD 提交的那份报告字节，不是任何重算；`F_recheck` 证明重取与 E 同代。
* `I_*` 只重放 2/15 行（每族一行，另 13 行不冒充）；`J_*` 只量 government_us 一条腿的 seam 盲区，形状上是 `shared_http_client` 类的问题，未对 `governed_sdk_session` 复验。
* identity_set 在前后两代都不动（稳定投影，不含 status），代际证据是 `STALE 317 → 0`、`accepted 317 → 332` 与 `INCOMPLETE 22 → 7`。
* 探针写过的路径只有：`/tmp/w33_landing_radius/**` 与本 bundle 的 `runs/w33_landing_radius.txt`；`docs/evidence/C101/**`、notes 与链载档字节未动（`c101_root_seen=False`，`:24`）。
* 本面没有跑任何 producer/recorder/merge，也没有带 in-repo `--output` 跑判定器。

## 12. 预测判据（下一面必须读到）

**谓词**：若下一波落 tmx 的 OptionsChains + IndexSnapshots：它必须先在 `iter2_claims_fixtures.py` 里把 HTML/CSV 传输声明成第四种 seam kind 并给 allow-list（复用 GraphQL envelope 是假落地），预测平面 1 `332 → 334`、`E` 型 mover 只有 `{'tmx': 2}`、census 的 `implement_missing_model 4 → 2`、且 counterfact 重放读到的拒绝站点落在新 fetcher 自己的 `transform_data` 而非 helper。
**失效读数**：任一侧 `regressions != []`；或 re-sign 后 `STALE != 0`（domains.yaml pin 又没付清，这一代的价格是 `:6` 的 317）；或 `residue + accepted != 350`；或 seam kinds 出现第四类但 `rows_without_witness_entry != 0`。
**失败读数**：下一面 `arms_failed > 0` 且臂名不在 {`census_flip_residue_18` 的 B_landed_negative 归因}——census 那把红臂在它被正式 re-arm（C137 `w32_census_control_rearm.py` 的形状，且不写 C128 历史档）之前应保持现状；任何把 `arms_failed` 归零却动了 C128 字节的"修绿"，按伪造处理。
**关于 `allowed_hosts` 盲区（§7）**：任何把 TreasuryPrices 或后续行推进 live 腿的波，必须先让 grant 的 host 表在请求路径被消费或显式声明 witness 例外；预测验证形状 = 在 `request_budget.py:459` 同处对 offline 腿读出 `HOST_NOT_ALLOWED` 或读出刻意的豁免注释，二者皆无则该发现仍然开着。
