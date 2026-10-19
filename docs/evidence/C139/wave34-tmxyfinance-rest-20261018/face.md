# C139 wave-34：把 wave-33 判为"落不了"的 4 行重测——3 行落地、1 行确认不可落，另有一条我自己的退档

一句话：平面 1 **332 → 335 / 350**（claim 1695 → 1700，`{'INCOMPLETE': 5, 'OFFLINE_ACCEPTED': 335, 'UNREVIEWED': 10}`），落地的是 `tmx::OptionsChains`、`tmx::IndexSnapshots`、`yfinance::FuturesCurve`——三行此前被我以"没有 envelope 形状 / 多主机 / 只走 SDK"判死，本波实测三条形由不同：形状可表示、主机可声明、模块存在。同时本波**撤回了我自己的一次实现**（`yfinance::KeyMetrics` 改走快照面），因为它过了五相却违反了仓内两条测试钉住的按期次语义；`yfinance::AvailableIndices` 的 0 端点判定则由推测升成实测。残差 **15**：`{'implement_missing_model': 1, 'ledger_ruled_out': 11, 'new_provider_package': 2, 'shard_entry_needs_harness': 1}`（`PYTHONPATH=<repo> py313 docs/evidence/C128/…/residue_axis_census.py` 的 `2_actions`，本波跑于落地之后）。

## 1. 仪器与判定源

判定器与平面读数一律 import 仓内实现：`scripts/quality/iter2_model_evidence.py`（`judge.build_report` 进程内），残差分类 import `docs/evidence/C128/…/residue_axis_census.py` 与 `incomplete_row_guard_check.py`，pin 走查 import C135 step6 的 walker；出货解释器 `/Users/yunjinqi/opt/anaconda3/envs/py313/bin/python`（yfinance 0.2.66 / pandas 2.3.1）。链载档在本 bundle `runs/`（61+ 把，逐档自带 argv 与 `<name>_exit=`）。

本波为什么用 wave-33 的链驱动：`probes/w33_run_chain.py` 是唯一在树的驱动，我把它当模板复制到本 bundle（`probes/w34_run_chain.py`）并按 provider 循环——**这个决定造成了两处本波自己的缺陷，见 §7 第 1、2 条**。

## 2. 面 1：三行的"不可落"被实测推翻

| 我原先的说法 | 实测 | 落点 |
| --- | --- | --- |
| OptionsChains "需要 `read_html` + EOD `read_csv`，无 envelope 形状" | 出货 fake 用 `response._content` 填 body 并按 spec 设 `Content-Type`，HTML/CSV 就是普通 envelope；2 腿 1 主机（`www.m-x.ca`） | `runs/producer_tmx.txt` 五相 PASS；fixture sha `bfc04812c598a2e2…`；反例 `TmxProviderError/TMX_BAD_ROW`，traceback 终点 `fetchers_w34_envelope.py:392 _exact_date` |
| IndexSnapshots "3-4 腿跨 3 主机" | 一份 spec 是 `responses` **列表**（`url_contains` + `sequence`），多主机有 `ALLOWED_DIRECTORY_HOSTS` 先例；实测 2 腿 2 主机（`tmxinfoservices.com` JSON + `app-money.tmx.com` GraphQL） | 五相 PASS；fixture sha `5074a919ad55780a…`；反例站点 `fetchers_w34_envelope.py:347 _json_number`（经 `_number_cell:746`）；无报价指数证明按存在性丢弃（`:278`） |
| FuturesCurve "只走 SDK" | 0.2.66 `quote_summary_valid_modules` 含 `futuresChain`，`YfData.get_raw_json` 存在（实测 `hasattr` 两项）；真正的缺口是本仓没有域键与契约类 | 五相 PASS；`seam_requests=3 rows=3`；反例 `YfinanceProviderError/YFINANCE_FUTURES_CHAIN_EMPTY`，站点 `futures_curve_iter2.py:156 _chain_symbols`；4/4 字段，未标到期日那行 `price=None` 让可空性被跑过而不是被声称 |

契约层的做法与 wave-33 一致：先落 `domains.yaml` 三个新键（338→**356**，`tmx_options_chains` 55 字段 / `tmx_index_snapshots` 26 / `yfinance_futures_curve` 4）与 `opendata/data/models/` 的契约类，再让实现体填进各自独立模块（`fetchers_w34_envelope.py`、`futures_curve_iter2.py`），witness 只写 `_parts/<provider>/rows_w34_*.py`，父模块不动——两批实现因此可以并行而不撞文件。字段表用 `scripts/quality/upstream_model_specs.py --output /tmp` 机器核对（350 models，upstream commit `3e071fcc2cd9`），不是照 note 抄。

**诚实的可空性账**：OptionsChains 产出 **25/55**、IndexSnapshots **20/26**，未落字段全部列在 shard 的 `UNSOURCED_FIELDS`（greek/size/tick/mark 等 30 格在两个 envelope 里都没有对应单元格；`underlying_price` 上游要靠第三个 GraphQL 报价，EOD 代理值不是 underlying 价 → 拒绝充数）。契约允许这些格为空，所以五相能过；但"契约允许空"不等于"这行的语义完整"，这两句分开记，别合并读。

## 3. 面 2：我撤回的一次实现（KeyMetrics）

一个并行 agent 把 `_sdk.require_valuation_measures`（1.7.0 的方法，0.2.66 确实没有）改走 `Ticker.get_info`，五相全绿（`seam_requests=4 rows=1`，反例 `YFINANCE_METRICS_SNAPSHOT_UNMAPPED`）。我核对后**全部回退**，证据三条：

1. `tests/test_yfinance_iter2_market.py` 从绿变 **38 failed**，`AttributeError` 指向被我删掉的 `_sdk` 出口；
2. `test_key_metrics_public_ttm_period_and_limit_mapping` 钉的是"frequency/periods 透传给 SDK"且逐行 `period_kind` 组成一个集合（`:1614-1615`）；`test_key_metrics_rejects_unmappable_fiscal_quarter_before_io` 钉的是 **IO 之前**的 `ValidationError` 与 `calls == []`（`:1627-1631`）。快照面给的是 `period_kind="current"` 且无期次列，等于把行的语义换掉来换绿；
3. 回退后该模块 **112 passed**，`git diff` 归零，agent 留下的 shard 删除。

所以我此前"SDK 缺口"的标签在**结论上成立、在理由上错了**：不是 0.2.66 没有 key-metrics 面（`info`/`fast_info` 有），而是它没有按期次的 valuation-measures 面，而契约要期次。落这一行需要的是 SDK 版本决策（把 pin 升到带 valuation-measures 的版本），不是换 transport。这条现在挂 `shard_entry_needs_harness`（census 仍读 `PRODUCIBLE_NOW`，见 §6）。

## 4. 面 3：`AvailableIndices` 的 0 端点——从推测升成实测

上游 `available_indices.py:59-66` 是 `DataFrame(INDICES).transpose().reset_index()`，`INDICES` 来自 `references.py:52`，**不发任何请求**；本仓既无 `yfinance_available_indices` 域键也无契约类。离线 witness 的强制臂是"这条腿自己至少问 carrier 一次"（`_seam_guard` 与 `_run_direct`），0 腿 carrier 没有任何东西能让反例触及。结论：这一行不是"还没写"，是**当前验收仪器在形状上无法验收**——要么给它一个可声明前提的 reference 面（例如把 `INDICES` 当成被钉的输入并让它可反例），要么账面判掉。我没有擅自放宽判定器。

## 5. 面 4：账面拒绝里有一条不是证据

`runs/w34_ledger_ruling_face.txt:47` = `ledger_ruling_face arms=8 arms_passed=8 arms_failed=0 failed=[] residue=18 ledger_ruled_out=11 class_co… PASS=True`（该面取于本波落地前那一代，`residue=18` 是当时的读数；判定 import 出货 `_ruled_out` 与 census，不手抄判定串）。分类：**a=2**（cboe::FuturesCurve `outside.bespoke`、seeking_alpha::CalendarEarnings `outside.anti_bot`，可测）、**c=6**（结构原因：flow.branch / fan_out+join / compose+assemble / preflight_lookup+columnar / object_keys）、**d=3（全部 wsj 三行）**。三条 wsj 的理由是 `dict.fromkeys` 把**同一句散文**摊到三个键上（`distinct_reasons_in_dict=1`），而这三个行的实测能力需求（`request.serialised_param`、`rows.order_limit`）在盘上**都读得到锚点**；另外 11/11 的 `upstream_shape_file:line` 都指向被定义的 fetcher，所以"上游没协议"不是上游缺失事实。空输入造成的假零检查过：`ruled_out_rows_through_an_empty_dict=0`、`census_keyed=11/11`、`residual_unexplained=0`，三个空 dict（federal_reserve/multpl/tiingo）证明不拒任何行。为能的对照 52 档同 provider 行读"未被拒"。**结论：这 3 行按本目标的口径还不能算"已判定"，需要 owner 逐行给可测理由或让我实现。**

## 6. 面 5：主机声明与它的强制边界

`scripts/quality/iter2_witnesses/tmx.py:122-138,399` 的 `allowed_hosts` 原先只有 `app-money.tmx.com`，而本周落地的 11 行实际发往 4 棵别的机器；`government_us.py:105-131,340` 同样漏 `treasurydirect.gov`。已改成从 provider 侧常量派生（`ALLOWED_DIRECTORY_HOSTS` + `REQUIRED_HOSTS` + `TREASURY_*`），零重打字面量；`money.tmx.com`/`www.usa.gov` 排除（元数据与一个发布单元格，从不发请求）。 resolved 后 tmx 5 主机、government_us 5 主机，改后 25 相全 exit 0。

边界按实测报，不掩盖：**离线 carrier 不校验主机**。`iter2_claims_probe_runner.py:1039` 是唯一产生 `entry["hosts"]` 的地方（`grep '"hosts"' _parts/**` 0 命中，父模块无条件赋值，所以 shard 里写 `hosts` 是死配置）；`_FakeCarrier._respond`（`:317-345`）只按 `url_contains` 匹配、不读 `ctx.request_budget`——把 `allowed_hosts` 在内存里换成 `sinkhole.invalid.example`，`EtfInfo`/`OptionsChains`/`TreasuryPrices` 的 direct 仍 `VERDICT=PASS`。真路径确实校验（`request_budget.py:566` → `_matching_grant:457-459`，caller `http_client.py:472`），我的 arm A 里 5 棵授权主机全过、伪主机 `RequestAuthorizationError`。**即离线面的"只碰声明主机"目前由 fixture 针脚绑定，而不是由声明强制。**我没有为了让面相绿去改判定器。

## 7. 撤回与更正（本波自己的三条，留数不静默覆盖）

1. **我让 61 把链载档覆盖了 wave-33 已提交的同名载档**：复用 `w33_run_chain.py` 也复用了它的 `runs/` 文件名与 `BUNDLE` 常量。发现时 `git status` 有 63 条 C138 变动。处置：把这一代读数复制到本 bundle（`runs/` 61 把 + `chain_summary.txt`），再 `git checkout -- docs/evidence/C138/` 还原 wave-33 的字节（现 `C138_dirty=0`，其 face.md 引的行号仍然成立）。教训：驱动必须把 bundle 与载档名当参数，不能继承上一波的字面量。
2. **我允许的磁盘反例与签名链抢同一棵树**：一个只碰 `tests/` 的 agent 用它那条 "forge one PINNED source file（append 1 byte to `domains.yaml`）" 的对照臂在盘上改了 `domains.yaml` 又还原；当时链正在跑，23:28:21 那三张 fmp receipt 记到了被篡改的 digest `550a3cf0b961`（同批别的行记的是 `c40055251746`），于是判定读数为 **331 accepted**，其中 `fmp::EsgScore/EtfCountries/FinancialRatios` 报 `contract_pin_2_current_hash_drift` + `primary_review_missing`。修法不是重签全部：定向重跑 `producer_fmp → recorder_fmp（signed=69 refused=0）→ producer_yfinance → recorder_yfinance（signed=27 refused=0）→ merge → judge`（`runs/w34_fix_rerun.txt`，全 `_exit=0`，judge `350 identities, 335 offline accepted`）。**规则：链在跑时禁止任何磁盘写入型对照臂，反例一律走内存或 scratch 副本。**
3. **`yfinance::FuturesCurve` 差点因为上一波的 `--models` 名单没更新而漏掉**：驱动沿用了 wave-32 的 26 行白名单，链跑完是 334 而不是 335；发现方式是拿 HEAD 已提交的判定报告与新报告逐身份对账（`lost=3 gained=2`），`gained` 里只有两行 tmx。上面那次定向重跑把名单扩到 27 行一并补掉。

## 8. 本波没落什么（带分母）

残差 **15**：`ledger_ruled_out` 11（其中 3 条理由不是证据，见 §5）、`new_provider_package` 2（biztoc::WorldNews、stockgrid::ShortVolume：`globbed=0`，整包落地 + `catalog.py` 全树改动；rights 侧不是闸门——`openbb_inventory_plane` 的 RIGHTS LINK 只在 `if served:` 触发，注册是 `docs/data-rights-registry.md` 新增编号行 + `provider-inventory.yaml` 的 `rights_rows` + CSV 两行的 `rights_status/rights_evidence_ref`，责任人见 `RIGHTS2-biztoc`/`RIGHTS2-stockgrid`）、`implement_missing_model` 1（`yfinance::AvailableIndices`，0 端点，见 §4）、`shard_entry_needs_harness` 1（`yfinance::KeyMetrics`，SDK 版本决策，见 §3）。provider 覆盖 **28/32**，零验收仍是 biztoc / seeking_alpha / stockgrid / wsj。`witnessable_today=0`。

## 9. 下一波预测判据

**若 owner 判 wsj 三行落地**：预测平面 1 `335 → 338`，代价是 3 个新 `domains.yaml` 键（全树前提，本代 = 335 行重签，读数形式是 `stale=335` 而不是 `stale=3`）；`witness_module_rels` 只钉 provider 前缀，`provider_local` 半径按本波实测是"该 provider 已验收行数"。失效条件：落地后 `regressions != []`，或 census 的 `residue + accepted != 350`，或 `E_seam_kinds` 出现第四类 seam 而 `rows_without_witness_entry != 0`。
**若 owner 决定升 yfinance pin 以落 KeyMetrics**：预测 `335 → 336`，且**必须**在同一面里读到 `test_key_metrics_public_ttm_period_and_limit_mapping` 与 `…_rejects_unmappable_fiscal_quarter_before_io` 两档仍绿（它们是期次语义的唯一钉），只有五相绿而这两档红即为换语义过关，判 red。
**若 owner 判 `AvailableIndices` 用 reference-input 面验收**：先决条件是给出一个能反例的钉（`INDICES` 常量被改一个键 → 行集变化必须被拒）；没有这把仪器前，我不接受"registry 里有名字"当作验收。
**关于离线主机声明**：任何把 `_FakeCarrier` 扩成校验 `allowed_hosts` 的改动都是判定器变更，必须自带两面（漏声明的主机 → 拒；已声明 → 过），并把 25 相回归跑齐。

## 10. 静态面与提交载荷

本波改动的静态面（`ruff check` / `ruff format --check` / `mypy` / `bandit -c bandit.yaml`，一律仓根相对路径）在 agent 面与我的复核下为 0 finding：产品 `providers/tmx/models/{fetchers_w34_envelope.py,client.py}`、`providers/yfinance/models/futures_curve_iter2.py`、`models/{tmx.py,yfinance_iter2.py,__init__.py}`、`domains.yaml`、witness `_parts/{tmx,yfinance}/rows_w34_*.py`、父 witness `tmx.py`/`government_us.py`、以及 `tests/` 的四份钉表。测试面（`-o addopts= -p no:cacheprovider`）：`tests/test_yfinance_iter2_market.py` 112 passed（回退后）、七个 census/authority/alert 模块 **165 passed / 0 failed**、`tests -k "tmx or government_us"` 140 passed。

提交载荷与凭证收口按本仓规矩：扫的是 index 里的 staged blob；权威档数由终端那把扫描自己的 `staged_files=` 给出，本节不替它自数；提交之后紧跟的证据提交带对 HEAD 的逐档 digest 对账与针对该提交的 gitleaks + 出货 `secret_scan_check`。仪器沿用 wave-33 那把并把 `WAVE_PREFIXES` 扩到本波的前缀集（`opendata/data/models/`、`providers/{tmx,government_us,yfinance}/`、`docs/evidence/C139/` 与四个测试文件）。
