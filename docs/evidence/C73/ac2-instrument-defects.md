# C73 — AC2 平面在落档前修掉的仪器缺陷

本轮交付是 `scripts/quality/ac2_case_probe.py`：§2 的 25 条散文判据，逐条变成「测量面 + 判定 + 闭合读数 + 反例」。
在把任何读数当成迭代2 的验收结论之前，每个面都先对着仓库真实形态验一遍；下面 12 处是仪器自己错了的地方。
它们的共同危险是：面读到的 0 不是「不合格」，而是「我没看对地方」，于是把仪器缺陷写成产品结论。

每条给出：错误的面 → 它漏掉的真实面 → 修正后的读数（全部来自本轮 `docs/quality/ac2-case-ledger.json`）。

| # | 用例 | 仪器原来的错误 | 修正后的面与读数 |
| - | ---- | -------------- | ---------------- |
| 1 | AC2-02 | `declared_fetchers` / `reused_fetcher_classes` 两个键根本没有被计算，判定拿到的是无意义比较 | 逐 descriptor 展开 fetchers 再按 `type()` 去重：`descriptors=34`、`capabilities=46`、`declared_fetchers=46`、`reused_fetcher_classes=0` |
| 2 | AC2-04 | 分母读的是 `docs/evidence/C65/port-scope-manifest.json`（327 行/531 处 import/2 处字符串/9 行 manual），那是逐文件 port 报告的口径 | 改写分母取 `基线快照.json.legacy_imports.per_file`：`census_files=166`、`census_rows=166`、`census_row_sum=532`、`census_line_sum=532`、`census_statements=532`、`census_sha_rows=166` |
| 3 | AC2-04 | 只看 manifest 与磁盘是否一致（166 行不一致），无法说明这 166 行是不是登记过的那批改写 | 加双向对照：`stale_pin_outside_census=0`（每条陈旧 pin 都在登记的改写里）、`census_file_without_pin_change=0`（每条登记的改写都在 pin 里）、`same_line_rewrites=165` + `line_count_changed_rows=1` |
| 4 | AC2-06 | 查 `[tool.setuptools.package-data]` 时用目录路径当键；该表的键是**点号包名**，于是 7 个资源包全部查空 | `covered_by_package_data=7`、`resource_files_on_disk=7`、`resource_files_total=7`、`uncovered_paths=-` |
| 5 | AC2-07 | 我凭空加了一条「档案记录的根应等于 vendored 路径」的判定。`PORT_ROOT=opendata_http` 是证据身份空间，重定位由 `PORT_ROOT_NOW` 承担，这条判定与仓库设计相反 | 删除该判定，换成三条真面：`baseline_scope==declared_scope`、`empty_scope_buckets=0`、`walker_detail=opendata=310, _vendor=325, opendata_client=2`（`walker_count=637==baseline_scope_sum`） |
| 6 | AC2-11 | 「原有 12 条能力存活」这条查的是不存在的 fact 键，永远读不到东西 | `p0_localized_verified_caps=12`、`p0_inventory_fetchers=85`、`p0_rows=85`、`p0_registry_caps=19`、`p0_provider_names=ecb,fred,imf,oecd,yfinance` |
| 7 | AC2-16 | `cold_import_probe` 的输出是多行，直接 `json.loads` 失败后被 except 吞成 `{}`，对照臂等于没测 | 只取 `ARMS ` 前缀那一行：`spec_arms=[2, "rejected", "accepted"]`——同一模块在两条臂里给出不同值，对照臂才真的能不同 |
| 8 | AC2-16 | 我凭空加了「unsupported 这个符号被用到过」的判定；文档要求的是**在 I/O 前拒绝**这个行为 | 换成行为面：`unsupported_refusal_tests=6`（`UnsupportedAsyncFetcherError` 在 tests 里的出现次数），并保留 `unsupported=0` 作为未声明适用性的行 |
| 9 | AC2-17 | 路由普查只读了 5 个 API 模块中的 1 个，并且要求路径字面量与装饰器同行；4 条 `@router.post(` 换行写路径的路由因此不可见 | 改用 AST 遍历装饰器：`api_model_routes=6`（原读 2）、`api_route_modules=5`、`api_route_verbs=get:2,post:4`、`api_routes_without_path=0` |
| 10 | AC2-22 | 重试面 `count_needle("retryable", ["opendata/data/providers/_engine"])` 读 0，而 `_engine` 里从来没有这个面；可重试性判定与上界在请求路径上 | `retryability_gates=2`、`retry_gate_files=opendata/data/http_client.py,opendata/data/providers/ths/transport/http_client.py`、`attempt_bounds=3`（`max_attempts: int = Field(default=3, gt=0)`、`MAX_SOURCE_ATTEMPTS = 3`、`for attempt in range(1, self._max_attempts + 1)`） |
| 11 | AC2-24 | 判定要求「130 个 § 标题」，把台账条目数当成了文档标题数；文档实际按 19 个 AC 分组 | `iter1_ac_headings=19`、`group_heading_symdiff=0`（标题集与条目分组集对称差为 0）、`group_counts_sum=130==ledger_items==items_total_field` |
| 12 | AC2-25 | Makefile 成员正则不匹配 TAB 缩进的 recipe 行；gate 记录的 glob 与文件名约定也指错，导致记录集为空 | 正则改为 `^\s*@\$\(MAKE\) --no-print-directory ([a-z0-9-]+)`，记录面改为 `docs/evidence/*/gate*.txt`，并按 `HEAD=<7位 sha>` 只取同树记录；同树选择必须能区分「旧一轮的绿色」与「本轮冻结树」 |

## 文档侧的一处（不是仪器，但它会让仪器少读一行）

§2 的 AC2-24 行里 `AC-1|10` 未转义，markdown 把这一行切成 5 格；`parse_cases` 会把它报成 malformed，
分母就少一条。改为 `AC-1\|10` 后 `ac_1_10_cell_count=1`、`ac_1_10_cells=AC-1|10|0c44a7ef`。
转义/未转义两侧都写进了 `tests/test_ac2_case_probe.py`：转义行必须解析成 4 格且判据里保留 `|`，未转义行必须被报出来而不是被静切。

## 我自己在这轮里写错并被当场抓到的东西

* AC2-25 的重写里出现过两个臆造：一个未定义的 `REPLACE` 常量（异常被兜成 NO_FACE），以及一句
  `facts["gate_record_path"].split("/")[2][:0] + "1"` 这样的无意义比较。两处都是我先相信了自己的输出，
  再被 `--self-test` 的 closure 臂抓住。
* 一次批量补丁脚本在 `assert` 处中止而没走到 write()，我当时以为文件已经改了。后续改用 Edit 逐处落盘，
  并以「面变化」而不是「没报错」作为写入证据。

结论：这 12 处若有一处留在判定里，本轮就会把仪器缺陷写成迭代2 的验收结论。修完之后，`--self-test` 打印
`SELF_TEST PASS failures=0`（25/25 的 closure 臂到 holds_offline，136 个反例逐个翻转：
`ac2-selftest.txt` 里 25 行 `breaks=n flipped=n` 之和），`--all` 的读数才可作为 §2 的判定面。
