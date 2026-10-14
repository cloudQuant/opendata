# C101 迭代2 收尾：registry_batch5 身份普查 re-base（判定探针 + counterfact）

commit-under-test HEAD = `270e373411951be1752d71206379da98762d5081`
判定探针 = `probes/verify_census_rebase.py`，读数 = `verify.txt`（`CENSUS_VERIFY_EXIT: GREEN`，18 面 PASS，
`census_verify_failed=0`；文中唯一 "FAIL" 字样出现在 counterfact 行的 *turns 2 face(s) FAIL*，不是失败面）
反例读数 = `arms.txt`（`ARMS_VERDICT: OK`：4 个 control 全 PASS、4 个篡改全 FAIL、5 个文件 byte-exact 还原）

## 1. 这次 re-base 动了什么（只动普查，不动语义）

用户裁定 = **Re-base census, freeze semantics**：摘要/计数/成员清单/快照可由活体重算，但必须打印 delta
并留下可证伪臂；retraction policy、refusal codes、契约类型、`verified` 旗标、已对照转正标签一律字节冻结。

活体普查（`verify.txt` 的 `live census` 行）：

| 面 | batch5 定格值 | 迭代2 活体值 | 差 |
| --- | --- | --- | --- |
| domain_count | 337 | 338 | +1 |
| query_semantic_domain_count | 317 | 318 | +1 |
| provider_model_identity_count | 350 | 351 | +1 |
| registered_source_domain_leg_count | 350 | 351 | +1 |
| deferred_source_domain_leg_count | 317 | 318 | +1 |
| legacy_source_domain_leg_count | 33 | 33 | 0 |
| descriptor_eligible_identity_count | 未定格 | 329 | 新字段 |
| model_key_only_identity_count | 未定格 | 22 | 新字段 |

导出面（`opendata.data.models.__all__`）另见 `iter2_census_rebase`：330→329 / 326→325 / 325→324，
归因 = 我删掉 2 个重名孤儿类（HEAD 为 331，删 2 得 329），+1 个本轮之前就已存在的导出漂移记为不可归因。

## 2. 身份增减的真相（锚在已提交的 batch5 定格值上）

`registry_census_rebase.identities_added` 早期一版只写了 finviz 一行，因为它拿的是我会话中段的临时快照
（digest `294bb5b0…`）做基线。现改锚 `git show HEAD:` 的定格值（digest `7ce0ca61…`，探针自取，不依赖 /tmp）：

- added 4：`finviz/EquityScreener/finviz_equity_screener` + `federal_reserve` 的
  CentralBankHoldings / InflationExpectations / TotalFactorProductivity（域名换成带源前缀的迭代2 域）
- removed 3：上述三行在旧域名下的同一 (source, model)
- 净 +1 = 351，counterfact 臂 `identity_delta_equals_added_minus_removed` 为真
- 三行是*改名*不是新模型，已在 `identity_renames` 里逐行写明

## 3. 字段位移：普查位移 vs 语义位移

对同一批共同身份逐字段比对（`verify.txt` 的 `moves vs committed batch5 pin`）：

- `binding` 位移 10，全部在 federal_reserve（clean_rates/clean_documents → 迭代2 逐模型模块）
- `scenario_present` 位移 29（imf 8、federal_reserve 10、finviz 2、fred 2、eia 2、intrinio 2、sec 1、cftc 2）
- **`contract` 位移 0，`verified` 位移 0** ⇒ `semantic_field_moves` 钉在 0，非 0 即越界（探针面
  "semantic fields moved 0 vs batch5 pin"）
- `scenario_present` 总数 309 → 342，闭合式 = 29 个既有身份位移 + 4 个新增身份自带 scenario（探针面
  "scenario total explained by attribution" 逐值复核 `scenario_present_total`）

## 4. 与已评审记录的冲突：只登记，不翻案

`approved_delta`（SLOOS 的评审记录）里 `scenario_present=False` 与 `scenario_policy` 散文一律**字节不动**
（探针面 "approved_delta still reviewed-absent scenario"、"frozen adjudication block byte-identical"）。
活体现在有 scenario 字符串，来源是迭代2 的 34 个消费用途回填（描述性文案，不是 C65 批准用途）。
冲突写进 `approved_delta_conflict`：`c65_use_case_approved=false`，`verified` 仍 false，`resolution` 记的是
"recorded, not flipped"。测试里对应断言把 定格假/活体真/未获批 三件事分别钉住。

## 5. 新增普查字段为什么必须存在

`canonical_ids_declared`（逐身份布尔）。规则：`catalog.register_providers` 只有在
`binding.canonical_model_ids` 非空时才调 `registry.register_provider_models`，所以
`list_model_descriptors()` 恰有 329 条 = 351 身份 − 22 条只走 model_key 的绑定
（akshare 10 / ecb 3 / fred 3 / imf 3 / oecd 2 / yfinance 1）。
其中 query-semantic 侧 318 条、legacy 侧 11 条且全部是 ths。

原来三处断言把 *全部 descriptor* 跟 *deferred（qsem）分区* 画等号（`tests/test_provider_domains.py`
`::test_declared_domain_contracts_match_exact_registered_provider_models`、
`tests/test_alert_matrix_model_domains.py::test_default_scope_...`），
即 HEAD 上就是 328 vs 317 的红，属既存红，不是本轮引入。修法不是放宽判据，而是把 descriptor 的总体讲对：

- descriptor 集合 == 已定格的 canonical-eligible 集合（329）
- descriptor ∩ deferred 分区 == `expected_registered_rows`（318，逐个仍走原 92+ 契约/verified/identity 校验）
- descriptor 的 legacy 余量必须全部落在 `legacy_source_domain_legs` 内、计数钉为 11、来源集合钉为 {ths}
  ⇒ 11 条 ths legacy 腿不会因这次改动而隐身

## 6. 消费方 census 字面量（337/317/350 → 338/318/351）

`tests/test_provider_domains.py` 9 处、`tests/test_pipeline_jobs.py` 8 处、`tests/test_alert_matrix.py` 6 处、
`tests/test_alert_matrix_model_domains.py` 5 处；`tests/test_provider_domains.py:1380-1384` 的
`batch5_count_adjudication` 字面量（350/317/337）**一字节未动**，由探针面
"frozen adjudication block byte-identical to the committed pin" 反向钉住。

未动：`tests/test_upstream_model_specs.py:265` 的 `models == 350`。它数的是**上游 OpenBB 树**里声明的模型数
（同一文件 `len(sheet) == 32` 是 provider 分母），与本仓注册表腿数不是同一总体，随本轮普查移动才是错的。

快照 digest 重钉两处：`BATCH5_SNAPSHOT_SHA256`（`test_provider_domains.py:1355`）与
`test_pipeline_jobs.py:1755`，均为 `3755f13aaba43989373aa44c6ca3f43fa9aabd2e0b2681b100628cac0d9a7bc3`；
探针面 "pins exactly the live fixture digest" 要求该文件里这两个位点各自只解析出这一个值。

## 7. 反例臂（`arms.txt` 逐行读数）

control（不动文件，必须绿）：descriptor / modeldomains / sloos / ods 四个测试全 PASS。

| 臂 | 篡改 | 结果 |
| --- | --- | --- |
| A | 把 `fred/SeniorLoanOfficerSurvey` 的 `canonical_ids_declared` 翻成 false | descriptor 测试 FAIL |
| B | 把 `ths/stock_daily` 的资格翻成 false | model_domains 测试 FAIL |
| C | 把 SLOOS 的 `scenario_present` 退回 false | production_domains 测试 FAIL |
| D | 把 `counts.domain_count` 退回 337 | ods 普查测试 FAIL |

收尾 5 个文件（快照 + 4 个消费测试）全部 `restored byte-exact`，最终 `ARMS_VERDICT: OK`。
探针内另有一条不碰盘的臂：deepcopy 翻一个资格位 ⇒ 同时翻红 2 面。

## 8. 两平面分母没有被这次 re-base 挪动

平面1 分母仍是 `模型级任务清单.csv` 的 350 行（`OBB2-finviz-EquityScreener` 本轮就在清单里，
注册状态由 NOT_REGISTERED 变已注册，所以注册表 +1 不是扩范围）。
清单 (provider, upstream_model) 对 vs 注册表 (source, model) 对是两个不同命名总体：
registry-only 33 对是 v1 遗留/model_key 绑定，inventory-only 32 对是 biztoc/cboe/econdb/seeking_alpha/
stockgrid/tmx/government_us/intrinio 等尚未注册的模型。两套总体各自计数，不做互相换算。
