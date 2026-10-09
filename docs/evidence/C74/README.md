# C74：把 port-scope 留档从「一次性脚本的遗留物」改成可重跑的生成面

日期：2026-10-09（Asia/Shanghai）。基线：`31acd14fa79d019fcd086c7d7a4e44070e4da8a0`（分支 `dev`）加本轮未提交工作区；C65、C73 的原始档案只读保留，未被覆盖。

用户要求：完成迭代 2 的开发与逐条验收；过程中按行业最佳实践发现需要改进的点，可以改进迭代 2 的设计或实现。本轮处理的是迭代 2 验收判定的地基：`docs/evidence/C*/port-scope-manifest.json` 这份 per-path 对账面。

## 为什么要有这一轮

`scripts/quality/port_scope.py` 只**校验**一份 port-scope 留档，不生成它。仓库里唯一的那份是 C65 写的，写它的脚本已经不存在了。迭代 2 改了搬运根目录之后，C65 那份 327 行里有 **166 行描述的是当前树里已经不存在的字节**，于是 `AC-5|02` 读 gap、`AC2-04` 的「人工差异可追溯」读到的是陈旧断言而不是当前事实。一个没有生成器的留档，等于一份不能复算的口头判定 —— 这一轮补的就是那条缺失的生成器：`scripts/quality/port_scope_inventory.py`（子代理撰写，主线程独立在磁盘上复核后采信）。

生成器按「真相住在哪儿」把每个字段分两面：

- **本轮重新测量**：pristine 上游对 `upstream.lock` 的摘要、搬运后对 `manifest.json` 与磁盘字节的摘要、行数、以及确定性 codemod 的逐路径重放（模块走 `port_source`，根 facade 走 `vendor_init_facade`，资源按原字节）所产出的改写计数与重放判定；
- **按 path 原样结转**：`reason`、`batch`、`scope_basis`、`ac6_02_existing_fixture_inventory` 以及录制的 fixture 清单 —— 它们是关于冻结范围与离线录制用例的决定，不是关于当前字节的陈述。`batch` 会经 `port_scope._expected_batches` 重新派生一次，但**只作交叉校验**，写进档案的仍是结转值。

失败即不写（fail-closed）：上游检出不在锁定 commit、上游/manifest/磁盘/C65 任一路径缺失、pristine 摘要离开过 lock、重放出的 `.py` 与磁盘不一致、结转的 batch 被派生拒绝、或总数不是 327/325/2 —— 任一触发则整个生成中止，不留半成品。对已存在的目标文件拒绝覆写，除非显式 `--force`；同一棵树重跑两次逐字节相同。

## 本轮留档读数（`port-scope-manifest.json`，逐字段从文件重算，非摘述）

- 行数：327 = 325 python + 2 resource；`reason` 非空 327/327；`manual_edits` 为真 9 行。
- batch 面：10 种不同的 batch 组合，322 行挂 1 个批次、5 行挂 2 个批次。
- `reconciliation` 逐计数：lock 记录 327（325 py + 2 res）、manifest 记录 325 + 2；lock↔manifest 路径集合相等 = true；上游摘要匹配 327/327；manifest 快照摘要匹配当前搬运树 327/327；python 行数匹配 325/325；`port_report` 行 327、路径集合与 lock 相等 = true、重放判定 PASS 327/327。
- 导入改写合计 237、字符串改写合计 2。
- `upstream_commit = c4f6a631c259783dbc2507b6b27d179b3e88079d`。
- 校验这份档案的面**不是** `port_scope.py` 自己：该模块没有 `main`/`argparse`/`__main__`（本轮实测：直接执行它不产出任何判定），它的 `audit_port_scope()` 是被验收探针调用的。可执行的校验面是
  `python scripts/quality/acceptance_item_probe.py --item 'AC-5|01' --item 'AC-5|02'`，本轮读到 `VERDICT AC-5|01: proven`、`VERDICT AC-5|02: proven`；后者打印「逐路径锁/manifest/磁盘核验 = yes（核对 327 条：325 py + 2 资源；问题 0）」，并把 1A/1B 批次的机器字段指向 `docs/evidence/C74/port-scope-manifest.json#files.batch`。
- 离线回归：`tests/test_port_scope.py` 12 例（校验器）、`tests/test_port_scope_inventory.py` 27 例（生成器：六条 fail-closed 分支各自断言异常类型、文档里的消息片段、`main()` 退出 1 且目标文件与父目录都不存在；正例是一棵合成的 327 行树，其产出自 `validate_port_scope` 且 `problems=()`）。子代理撰写，主线程逐个文件在磁盘上复跑后采信。

## 531 → 237：这句话不是口径松动，是一行的归属变了

C65 记 `port_report_import_rewrite_total = 531`，C74 记 237。逐 path 对齐后差值全部落在**一行**：`__init__.py` 从 294 变成 0，其余 326 行的计数逐行相同（294 = 531 − 237，路径集合两侧完全一致）。

原因是搬运根 facade 的实现方式：`scripts/codemod/port_module.py:763` 对 `_INIT_UPSTREAM_PATH` 走 `vendor_init_facade()` 而不是 `port_source()`，而 `port_module.py:549` 写的是 `payload, _report = _lazy_vendor_init(...)` —— 报告被丢弃，所以 `result.import_rewrites`（唯一在 `port_module.py:504` 计数、也是报告里那个 294 的来源）不再落到这一行上。**改写的字节仍然在档案里**：该行的 `manifest_ported_snapshot` 与 `port_tree_current` 匹配、重放判定 PASS。变的是这 294 次改写由谁来记账，不是它们有没有发生。

因此本档案的诚实口径是：237 是「走 `port_source` 的 326 个模块」的导入改写合计，根 facade 的改写数当前**无处记账**。已记账为待办、本轮不顺手改的理由（见下节）：把 `_report` 透出后，`__init__.py` 会重新成为一个「带改写计数的行」，而 `AC2-04` 的追溯条款刚被从「磁盘漂移集」这种瞬时载体重新武装到档案自身的 pin 对上 —— 计数器归属和判定载体必须同一轮一起改、一起复验，否则会把一个可满足的判定改回不可满足的假门。

## 本档案不包含的声明（避免被读数误读）

- **签名保真这一 leg 换了性质，但仍未闭环**：本轮之前，「实际函数名/签名/语义保真」在本档案里确实没有面 —— C74 记的是摘要、行数、改写计数与重放判定，不含签名 diff。本轮补上了对照面 `scripts/quality/ported_signature_fidelity.py`：对 lock 的 325 条 `.py` 逐路径比较 pristine 与搬运后每个 `FunctionDef/AsyncFunctionDef/ClassDef` 的 `(所在 def/class 链, 节点类型, 名称, ast.unparse(args))` 多重集，实测 `rows_compared=325`、`rows_equal=324`、`differing_total=1`，唯一不等的一行是 `__init__.py`；把该行的两侧 key 逐个落下来看，pristine 侧模块级 def/class 为 0 个，ported 侧多出的正是 `__dir__()` 与 `__getattr__(name)` 两个函数（搬运根 facade 由 `vendor_init_facade` 生成，没有丢失任何上游声明）。该 leg 在 AC2 台账里仍读 `blocked_leg`，但阻塞项不再是"没有对照面"，而是 `upstream.lock` 里这条记录的 `manual_edits` 仍为 `false` —— `differing_and_not_flagged_manual_total = 1` 就是这一处的计数。登记它要改供应商 pin 文件的元数据，本轮判为需用户确认的动作，未执行；AC2-04 的 closure 因此写成 `signature_differing_unregistered = 0`，即"这一处一旦登记，用例即闭合"，而不是把判定放宽。
- 无网络调用、未取上游实时数据（pristine 侧读数读的是本地 pinned clone 在 locked commit 上的字节）；未对数仓做任何 DDL/写入；未 `git push`。`ac6_02_existing_fixture_inventory.comparison_command` 里记录的命令**本轮未被执行**（`mode` 字段自述为离线录制回放的盘点，非执行记录）。
- 本轮**跑过并取了读数**的面（不要把它们当成"没动过"）：`scripts/codemod/gen_manifest.py --check`（AC-5|01 打印 exit 0 与原话 `OK: manifest.json is up to date`）、`scripts/quality/ported_signature_fidelity.py`、bandit 对搬运树的全量重扫（见下一节）、五个离线回归文件 12/27/18/11/16 例、AC2 平面 `--self-test`（25/25 通过，153 条 counterfact 各把干净读数打回一次）。
- 档案里 `files[].manifest.path_present`、`files[].port_report.path_present`、`files[].port_report.upstream_path_matches_lock` 三个逐行布尔**不是**独立读数：前两个是「生成器在这一步之前会因为路径缺失整体中止」的构造性真（`port_scope_inventory.py:138`/`:145` 写字面 `True`），第三个对 `port_report` 同理（重放的 upstream path 就是 lock 那条，`:146`）。真正的实测在 `manifest.upstream_path_matches_lock`（`:360` 逐行比较 `manifest_row["upstream_path"] == upstream_path`）和 `reconciliation` 的集合/摘要计数里。已记为待办：把这三处常量改成实测或删掉，避免后来者把构造性真读成核对过。
- 结转字段（`reason`/`batch`/`scope_basis`/录制 fixture 清单）的真值来自 C65 的冻结决定，本档案只保证它们按 path 逐字结转、并在 batch 上做了交叉校验。

## 复算方式

```
python scripts/quality/port_scope_inventory.py -o docs/evidence/C75/port-scope-manifest.json
```

写新目录（不覆写本轮档案），随后：

```
python scripts/quality/acceptance_item_probe.py --item 'AC-5|01' --item 'AC-5|02'
python scripts/quality/ported_signature_fidelity.py     # 一行 JSON，rc=2 表示对照没跑完
python -m pytest tests/test_port_scope.py tests/test_port_scope_inventory.py \
    tests/test_ported_signature_fidelity.py -q --no-cov
```

第一条是这份档案的校验面（`AC-5|02` 会打印它读的是哪一份档案与 `#files.batch` 字段），第二条是 AC2-04 那条签名 leg 的对照面，第三条是 12 + 27 + 18 个离线回归。`ported_signature_fidelity.py` 的退出码只表示"对照是否跑完"（检出可采信 + 每条锁内 `.py` 都双侧解析），**不**表示"有没有签名差异"；差异在 `differing_total` 与 `differing_and_not_flagged_manual` 两个计数里，判定面读的是计数而不是 rc。

`port_scope.scope_inventory_rel()` 解析**最新**的 `C*/port-scope-manifest.json` 并发布 `inventory_rel` 与 `batch_field = f"{rel}#files.batch"`，所以新增一轮档案会立刻改变所有读它的判定面 —— 这也是为什么 C65 的陈旧 166 行会一路影响到 `AC-5|02` 与 `AC2-04`。

## 同一轮的第二个档案面：搬运树 bandit 全量重扫 + 审阅逐条结转

`scripts/quality/ported_security_evidence_build.py`（本轮补的生成器）重扫搬运根并写 `docs/evidence/C74/ported-bandit-scan.json` 与 `ported-security-triage.json`；`scripts/quality/ported_security_evidence.py` 是它的校验器，`MAX_EVIDENCE_AGE = timedelta(hours=24)` 使这份 bundle 天然是**按轮**的 —— 下一轮验收的正确动作是重跑同一个工具，而不是放宽窗口或沿用上一轮。逐字段从档案重算的读数：

- 扫描面：`archive_round=C74`、`ported_root=opendata/data/providers/akshare/_vendor`、`python_files=325`、`scanner_version=1.9.4`、`python_version=3.11.8`、`scanner_exit=1`（bandit 有 finding 才返回 1，不是崩溃）、`source_sha256=0499005a027b710585d2ffe02f816ec49fbe78ef49516fca118f39b53a829def`。
- 审阅面：`finding_count=1048`、`high_count=31`、规则组 15 个且 `sum(各组 count) == 1048`（分类面可逆算，不是"看起来齐了"）、`scan_sha256=90947021be848d3c3c3f4c74aab0747d8636296fb14f3288bed6ca08b6096c7a`、`security_fixed=false`、`unresolved_risks=true`。
- 结转不是抄写：`carried_from` 记 `prior_round=C65`、`prior_root=opendata_http`、`prior_rows=1048`、`current_rows=1048`、`identities_matched=1048`（按「去掉 `opendata_http/` 的相对路径 + 规则 + 行号」逐条比对，增减均为 0），另有 `rule_citations_verified=4`、`javascript_citations_verified=4`；处置文本本轮未重写，所以它的真值仍来自 C65 的审阅决定。
- 判定面读数：`AC-5|07` proven（档案轮次标签现在是从 bundle 实测的，`archive_round_in_dir` 有对应的 counterfact）；`AC2-22` 由 `blocked_leg` 升为 `holds_offline`，8 条 counterfact 全部翻转。复算：`python scripts/quality/ported_security_evidence_build.py`（写进本波新目录，不覆写 A2/C65/C74）。
- 明令未做的：`make security-ported` 本轮不运行；没有修任何一条 finding —— 搬运保真按需求 FR-4 只要求「处置分类留档」，档案自己也写明它不替代漏洞修复或发布安全准入。

## 本轮自己造的一笔债，以及它把两个判定打成假门的过程

新工具 `scripts/quality/ported_security_evidence_build.py:26` 的 `import subprocess` 被 bandit 记为 B404，使 `bandit_selfdev` 从快照的 1 升到 2，于是 `scripts/quality/ratchet.py` exit 1、`scripts/quality/a2_check.py` exit 1；连带地，acceptance 平面的 `--self-test` 报

```
AC-17|03: no reading this probe declares can make the judge pass, so a gap here could never be closed by real work
AC-17|05: no reading this probe declares can make the judge pass, ...
```

原因链值得留档：棘轮在红的时候**不打印三项存量计数**，所以 `measure_ac17_03` 读到 `printed=0`、`cur_*='(absent)'`、`snap_*='(absent)'`，而它声明的 clean 读数写的是 `cur_* → *snap_*`（把当前值抄成快照值）—— 当快照值本身读成 `(absent)` 时，`0 <= number('(absent)') <= number('(absent)')` 永不为真，判定就成了不可满足的门。修的是债本身，不是门：按仓库既有写法（`ported_signature_fidelity.py:47` 同一行 `import subprocess  # nosec B404  # literal argv, shell disabled`）补 nosec 后，`ratchet.py` exit 0，五项存量 `ruff_selfdev 147(≤148, improved)`、`mypy_selfdev 0(≤0)`、`bandit_selfdev 1(≤1)`、`ruff_ported 1867(≤2188, improved)`、`direct_http_ported 1071(≤1071)`，`a2_check.py` exit 0，`AC-17|03` 与 `AC-17|05` 的声明读数复算为 proven。抬高上限从来不是选项 —— `AC-17|03` 的判据要求「被量的范围本身也只降不升」。
