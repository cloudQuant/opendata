# C73 — AC2 平面量出来的迭代2 真问题（不是仪器缺陷）

`ac2-instrument-defects.md` 记的是我这轮把仪器修对的过程。这一份记的是修对之后仍然立着的结论：
每条都只引用本轮 `docs/quality/ac2-case-ledger.json` 里的面，给出复算命令，不做口头判定。

状态口径：`holds_offline`=离线面成立但不等于结案；`gap`=离线就能看出的缺口；`blocked_leg`=离线成立、
缺一条真实_leg（live/warehouse/install/archive），缺口照登在分母内。本轮 25 条 = 2 holds_offline / 19 gap / 4 blocked_leg。

## F1 派生映射表用占位符顶替了模型身份（AC2-02 gap）

`opendata/data/openbb_map.yaml` 只有 17 行，而登记表里有 10 个本地源、34 个 descriptor 源。面：
`map_entries=17`、`map_missing_sources=3`、`missing_legs_named=bls,cboe,fmp`、`placeholder_openbb_models=13`、
`duplicate_openbb_models=0`、`map_ghost_providers=0`、`reused_fetcher_classes=0`、`second_registry_sites=0`。

13 条 `TBD` 不是「未填的表格」，是拿字符串顶替了身份：判据要求「每个已注册源都在映射里」，
所以 3 个源缺 + 13 个占位同时判 gap。注册面本身是干净的（46 capabilities / 46 declared fetchers /
46 个不同 fetcher 类，`declared_fetchers=46==capabilities=46`），问题只在派生映射。

复算：`python scripts/quality/ac2_case_probe.py --item AC2-02`

## F2 vendored 树替了门面，却没在任何地方登记这是一次人工偏离（AC2-04 gap）

三方身份本身对得上：`lock_python=325` + `lock_resources=2` == `lock_records=327` == manifest `325+2`，
`path_sets_equal=True`，`control_file_rows_in_lock=0`，`retired_root_import_nodes=0`。

分母也能逐行重算：`基线快照.json.legacy_imports` 登记 166 个文件、532 条旧 import 语句
（`census_files=166`、`census_rows=166`、`census_row_sum=532`、`census_line_sum=532`、`census_statements=532`），
而磁盘与快照 pin 不一致的正好是同一批 166 个文件（`stale_pin_files=166`、`manifest_vs_disk_mismatch=166`、
`stale_pin_outside_census=0`、`census_file_without_pin_change=0`）。

塌的是来源与偏离两处：
* `manifest_rows_with_rewrite_provenance=0` —— 325 行 manifest 里没有任何一行说明这条 pin 为什么与快照不同。
  「166 条陈旧 pin 恰好等于登记的 166 次改写」这件事现在只能靠外部脚本重算出来，档案本身不自证。
* `line_changed_unflagged=1`、`line_changed_paths=__init__.py` —— 该文件 pin 5830 行、磁盘 2001 行，
  `manual_edits: False`。这是一次门面替换（vendored 原文件被换成轻量门面），却登记成「与上游逐字一致」。
  其余 165 个文件是同行改写（`same_line_rewrites=165`），行数没变，被 `line_changed` 这条面正确放过。

按行业实践这不是「重新拉一次快照就行」：把 pin 改写成本轮实测值会销毁偏离记录。要做的是给 manifest
补来源字段并把这 1 处标成人工偏离，判定面才继续有牙齿。

两处口径要分开记，别在下一轮被谁「对齐」掉：`archive_import_rewrite_total=531`/`archive_string_rewrite_total=2`/
`archive_port_report_rows=327` 是 `docs/port-report.md` 的逐文件改写计数口径，`census_statements=532` 是
`基线快照.json` 的 AST 旧 import 语句口径。判定不要求 531==532（它们量的不是同一件事），
要求的是快照口径能逐行重算（166/166/532/532）且与陈旧 pin 集合双向相等。

复算：`python scripts/quality/ac2_case_probe.py --item AC2-04`

## F3 模型族缺一整层入口，覆盖状态 0 行有值（AC2-17 gap）

四层里 API、SDK、ODS、DWD 都有实物：`api_model_routes=6`、`api_route_modules=5`、
`api_route_verbs=get:2,post:4`、`api_routes_without_path=0`、`sdk_methods=4`、`ods_writer_symbols=3`、
`dwd_symbols=3`、`layer_flags_in_descriptor=1`。

塌的两处：`cli_model_commands=0`（`opendata/cli.py` 里没有任何 provider-model 子命令，
判据点名要 CLI），`coverage_status_assessed=0`（350 行的 `coverage_status` 全是 `NOT_ASSESSED`）。

顺带修掉一处我自己的仪器错误：路由普查原来读 2 条，是因为要求路径字面量与装饰器同行；
6 条里有 4 条是 `@router.post(` 换行写路径。改用 AST 后是 6 条。原读数会把「API 层缺失」写成结论。

复算：`python scripts/quality/ac2_case_probe.py --item AC2-17`

## F4 async 适用性只声明了 2/350 行（AC2-16 gap）

拒绝语义与边界是有的：`unsupported_refusal_tests=6`（`UnsupportedAsyncFetcherError` 在测试里的出现次数）、
`bounded_thread=2`、`engine_read_sites=7`，对照臂两条取值不同（`spec_arms=[2, "rejected", "accepted"]`）。
塌的是覆盖面：`rows=350`、`assessed=2`、`not_assessed=348`、`unsupported=0`。

判据要的是「逐行声明适用性并把结论落到执行语义」，所以 348 行未评估不能因为包装层存在而判 holds。

复算：`python scripts/quality/ac2_case_probe.py --item AC2-16`

## F5 迭代1 台账里 5 条证据指向迁移前的路径（AC2-24 gap）

台账自身是自洽的：`ledger_items=130`、`items_total_field=130`、`group_counts_sum=130`、
`iter1_ac_headings=19`、`group_heading_symdiff=0`、`proven=114`、`gap=16`、`ledger_check_exit=0`，
且 `ac_1_10_cell_count=1`（AC-1|10 的冲突单元没有被加成第 17 个 gap）。

塌的是可追溯性：`missing_evidence_files=5`，点名
`missing_evidence_named=opendata_fuyao/endpoints.py,opendata_fuyao/error_messages.yaml,opendata_http/datasets.py`。
这些是 A2/C66 搬迁前写下的路径，文件已不在原位；「证据存在」在这一格上是假的。
修法是逐格重新指认现路径的证据，不能改判定，也不能把 5 格降级成 gap。

复算：`python scripts/quality/ac2_case_probe.py --item AC2-24`

## F6 搬迁后的树拿不到安全证据，且这条腿本轮禁止重生成（AC2-07 / AC2-22）

* `ported_security_issue_count=1053`，码集为
  `scan-finding-path-invalid,scan-source-current-mismatch,scan-stale,source-tree-relocated,triage-source-current-mismatch,…`
  ——档案明确拒绝为已搬迁的树背书（`ported_security_declares_relocation=True`）。
* `preflight_status=ENV_BLOCKED`、`preflight_exit=1`、`preflight_issues=2`。
* 身份面本身成立：`declared_scope==baseline_scope`、`empty_scope_buckets=0`、
  `walker_count=637==baseline_scope_sum`（`opendata=310, _vendor=325, opendata_client=2`）、
  `zero_dep_exit=0`、`census_identity_holds=True`、`direct_http_ported_pin=1071`（债务是被钉住而非删掉）。

这不是可以就地抹平的读数：重生成 ported bandit/triage 档案要跑 `make security-ported`，
而它会覆盖 `docs/evidence/A2/bandit-ported.json`（历史留档面），本轮明令禁止。所以 AC2-07 记 gap
（其中工具链那一格是 ENV_BLOCKED），AC2-22 记 `blocked_leg`，`requires=archive_field` 写明 owed 的腿。
TLS 与重试面在 AC2-22 里都是绿的：`first_party_tls_disable=0`、`vendor_tls_disable=79`（vendored 风险被计数而非隐藏）、
`retryability_gates=2`、`attempt_bounds=3`、`raw_diagnosis_codes=13`、`throttle_handling=38`。

复算：`python scripts/quality/ac2_case_probe.py --item AC2-22`

## F7 开发面本身未完成（AC2-08/09/10/11/12/13/14 gap）

这些格子的 gap 是「活儿还没干完」，不是判定太严：`p0_dev_done=0`、`p0_live_verified=0`（AC2-11 的 85 条既有
模型一条都没到 DEV_DONE），AC2-10 的 350、AC2-08 的 delta/rights、AC2-09 的必需性逐模型、AC2-12 的 33、
AC2-13 的 120、AC2-14 的 112 同理。分母一个都没缩：`descriptors=34`、`inventory_providers=32`、
`rows=350`、`capabilities=46`、`vendor_py_files=325`。

## 已经站住的两格（正面读数，别在下一轮被我写坏）

* **AC2-01 holds_offline**：权威面写明 `authority_source=git show at fixed commit`、
  `upstream_commit==3e071fcc…`，重算与台账逐行对齐：`providers==expected_providers==32`、
  `models==expected_models==ledger_rows==hashed_rows==350`、`unique_models==expected_unique==202`、
  `duplicates=0`、`eia_models=2`、`eia_credentials=["api_key"]`、`credential_sources=15`、`issues=0`。
  该行的负边界「不执行上游实现」也是被评分的一条：`executed_upstream=NOT_EVALUATED` 若变成任何执行读数就判 gap，
  所以这一格结案不欠腿。
* **AC2-05 holds_offline**：`mit_header_files=325`（325 个 py 文件都保留 MIT 头）、`license_lines=21`、
  `vendor_independence_exit=0`，且冷导入有能真正区分的两条臂：
  `heavy_after_metadata_import=[]` 对 `heavy_after_app_import=["apscheduler","fastapi","loguru","opendata.core.config","sqlalchemy"]`。
  这一格的价值全在第二条臂：只测「元数据臂不拉重依赖」而没有对照臂，读数就没有意义。

三格 `blocked_leg` 的离线面同样是绿的，只欠声明过的那条腿：AC2-03（`archive_field`）、AC2-15
（`live_upstream`；`host_concurrency_limiter=11`、三层预算常量 `task/source/global_rate=True`、
`import_time_fetch=[]`）、AC2-21（`live_upstream`；`ids_match=True`、`accepted_status_only=True`、
`fe_422_case=True`、`fe_test_cases=12`）。欠的腿记在分母内，不改成 gap 也不改成 holds。
