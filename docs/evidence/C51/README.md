# C51 — AC-16\|07「P0 域集成测试」原来选不出任何用例：marker 注册而 0 应用 ⇒ 选择式选中 0 / 3358

本轮做两件事：**① 把 `AC-16|07` 那条「正向证据」判据从不可验证做成可判、可证、进门禁**（判据点名的那组用例此前**指认不出来**——`pytest.ini` 注册了 `integration` marker，全仓 0 应用，于是 `pytest -m "integration and not e2e"` 在开发解释器与干净 venv 里都选中 0 条），并以两个解释器各一遍的**全量不裁剪**留档作为正向证据；**② 把 `AC-16|06` 剩下的那一面量清楚并说明它为什么不再是一个开发项**（`import`／动态形态已为 0，差的只有「基线清零」，而抹平它的两条路都要改判据本身 ⇒ 登记为用户/产品决策）。AC-16 条目级 **2/9 → 3/9**；`AC-16|06` **不勾**。

## 〇、修前的真实形状（读自普查七面，全部只读、只测不写）

| 判据点名的面 | 已有的面 | 没有的面 |
|------|------|------|
| 「在未安装 akshare/openbb 的干净环境中 **P0 域集成测试**通过」（`AC-16\|07`） | `pytest.ini:23` 注册了 `integration`；`--strict-markers` 在 `addopts` 里；仓库里确实存在跨模块、打真引擎形状的用例 | **这组用例没有一个被标记**：`applied integration = 0`、`module-level pytestmark = 0 个文件`（普查面 5a），因此 `-m "integration and not e2e"` 选中 **0 / 3358 deselected、exit=5**，两个解释器读数一致（面 5b、面 7b）。一个选不出用例的选择式，其「全绿」是空的：0 条通过既满足「全部通过」也满足「一条都没跑」 |
| 「基线**只降不升**、新增即失败；A2 后**清零**」（`AC-16\|06`） | `make zero-dep-check` 两条命令（`--self-test` + 默认核对）在位且 exit=0；AST 走查 `import 形态 = 0`、`动态 import 形态 = 0`；扫描器双向自测咬合 | 「清零」这一面：`docs/quality/zero-dep-baseline.json` 仍冻 3 条（现场走查同为 3 条，形状是**名字面量**而非依赖，逐条归因见普查面 1） |
| 「C36 曾用什么顶替这条」（台账旧 reason） | `tests/test_ported_import_closure.py` 的 meta-path 阻断探针 | 它证明的是**依赖声明完备**（阻断 module-scope 依赖会炸、阻断懒依赖不炸），不是「P0 域集成测试在未装上游包的干净环境通过」。本轮**不用它顶替**，而是把判据自己那件事做出用例集 |

三点结构性事实（不是措辞问题，是判定面问题）：

1. **「注册了 marker」与「判据有了分母」是两件事**。`--strict-markers` 只对*用了未注册 marker* 的用例报警；一个注册却无人使用的 marker 在两个方向上都不出声。于是「P0 域集成测试」这句话在 8 个月里可以被逐字引用（v1 台账、`docs/evidence/C36/*`）而不被任何机器问一次「你指的是哪些用例」。
2. **「干净环境通过」必须是正向证据而不是推理**。可行的顶替论证有好几个（本机没装 ⇒ 通过即证明；或依赖声明完备 ⇒ 无需上游），它们都不构成判据要求的那件事。本轮的口径是：新建 venv，依赖面**恰为** `pyproject.toml` 声明集，装完后逐个回答 `importlib.util.find_spec` 三个名字是否可导入，然后在里面跑同一选择式。
3. **分母要有权威来源**。8 个单元不是从「哪些测试看起来像集成」里挑的：P0 域的名单取自 A4.1 迁移 `alembic_data/versions/20260923-0001_ods_dwd_p0.py` 的 `_DWD_TABLES`（5 个域），判定面断言「这 5 个域每一个都被被选中的用例点到」，缺一即红。

## 一、先量：七面，跑在活树上（`zero_dep_clean_env_census.py`，两遍留档）

| 面 | 它量什么 | 改前读数（`census-before.txt`）→ 改后（`census-after.txt`） |
|------|------|------|
| 1 基线归因 | 3 条冻结引用各自的形状与可达性 | 相同：`openbb_map.py:125`（`.get` 读的字段名）／`data_script.py:74`（`DataScript.source` 默认值）／`patrol.py:468`（`key_status()` 字典键）；三条 `reaches_import=no` |
| 2 活体走查 | 扫描面内 `import`／`dynamic`／`string` 三形态计数 | `import 0 / dynamic 0 / string 3` → 同 |
| 3 扫描器对照面 | 探测器的 8 条违规样本是否仍咬、4 条合规样本是否仍不咬 | 两遍都是 bites 6 / silent 4 / `--self-test exit=0`（探测器死了则上面所有读数作废） |
| 4 引用消费者 | 每个残留字面量在全仓被谁读（docs/tests/scripts 分布） | `openbb` 8 处、`akshare` 557 处；改后版本额外把站点按**具体那一行**归因（改前版本把 `akshare` 行重复印了一次） |
| 5a marker 普查 | 注册面 vs 应用面 vs 模块级 `pytestmark` | **applied integration 0、pytestmark 0 文件** → applied 8、pytestmark 7 文件（具名） |
| 5b/7b 采集面 | 同一选择式在开发解释器与干净 venv 里各选多少 | 两边都是 **0 selected / 3358 deselected / exit=5** → **151 selected / 3362 collected / exit=0**（两边逐字相同） |
| 6 域枚举 | 判据里「P0 域」在仓库内可能的分母来源 | `_DWD_TABLES` 5 域（权威）；`schedules.yaml` 4 作业里 2 个 p0 命名；`domains.yaml` 无 tier 字段 ⇒ 不能当分母 |
| 7 干净解释器 | venv 是否真的没有上游包、依赖图是否自洽 | `akshare=absent openbb=absent`、`pip check` 无破洞、129 个包、无上游名 |

## 二、改了什么（逐条对着判据）

| 面 | 改动 | 挣到哪一格 |
|------|------|------|
| 用例集接线 | 8 个单元纳入 `integration`：7 个模块级 `pytestmark`（`test_data_query_api`／`test_dwd_merge`／`test_index_constituent_asof`／`test_ods_writer`／`test_p0_providers`／`test_pipeline_run_api`／`test_pipeline_runner`）+ 类级装饰器 `test_pipeline_jobs.py::TestFullCheckExecutor`（该文件自称 Unit，只有这个类是跨边界打引擎的）⇒ 净 **9 行**，不改任何一条断言 | `AC-16\|07` 的分母 |
| 判定面 | 新增 `tests/test_p0_integration_surface.py`（4 条断言）：marker 注册且 `--strict-markers` 在位；被标集合 == 评审过的集合；5 个 P0 域都被这组用例点到；没有任何被标单元被 `e2e` 掏空（全标 `e2e` 会让选择式在 0 条上"通过"） | `AC-16\|07`、`AC-17\|10` |
| 条目级判定面 | `acceptance_item_probe.py` 新增 2 个探针（31→**33** 条）：`AC-16\|06` 7 个反事实面、`AC-16\|07` 10 个（含 `skipped`、`archive_blind`、`clean_here`、`archive_absent`）；面基线 `docs/quality/acceptance-probe-faces.json` +6 行，两格均**无 moment 面**（读的是已提交事实 ⇒ 轮中可判） | 普查面 7 的「proven/gap 而无判定面」集合 |
| 台账 | `AC-16\|07` → proven（round C51，指向两个解释器档案 + 构造档案 + 判定面），`AC-16\|06` 保持 gap 并换成量化 reason；两格随 §2 加性更正一起 re-key（`AC-16\|06\|2eb4fb66`、`AC-16\|07\|b17eac77`），§10 AC-16 行 2/9 → 3/9，banner v5.22 → v5.23 | AC-16 两本账对账 |

未改动（刻意）：`opendata/` 任何一行产品代码、扫描器的字符串规则、`docs/quality/zero-dep-baseline.json`、任何历史轮次档案、`.gitignore`、`pytest.ini` 的 marker 注册面（只补应用，不补注册）。

## 三、反事实面：四条断言各自能被单独打红（`guard-counterfacts.txt`）

| 突变 | 应红的那条 | 实际红的那条 |
|------|------|------|
| CF1 从 `pytest.ini` 撤下 `integration` 注册 | 断言 1 | `test_integration_marker_is_registered_under_strict_markers`（exit=1） |
| CF2 给被标的类去掉装饰器 | 断言 2 | `test_the_marked_set_is_the_reviewed_set`（exit=1） |
| CF3 给某个 P0 域模块去掉 `pytestmark` | 断言 2 + 断言 3 | 两条同时红（exit=1）——**这一条是第一版判定面的缺陷**：断言 3 原先读 allow-list 常量，CF3 打不红它，遂改成读活面 |
| CF4 把一个被标模块内所有用例再加 `e2e` | 断言 4 | `test_no_marked_unit_is_all_e2e`（exit=1） |

四条突变都在内存里留原文、逐字节回写，未用 `git checkout`；末行 `restored tree: exit=0 / 4 passed` 证明树复原。

## 四、正向证据：同一选择式在两个解释器各一遍（`clean-env-integration-run.txt`，正文 368 行不裁剪）

| 段 | 解释器 | 上游三包 | 读数 |
|------|------|------|------|
| 0b | 开发解释器 `~/opt/anaconda3/envs/py313` | `akshare`/`openbb`/`openbb_platform` 全 absent | 装的是 `.[web,dev]` 全集，不是"什么都能导入"的机器 |
| A | 同上 | — | **151 passed / 3216 deselected、exit=0**（real 8.86 s） |
| B | 本轮新建 venv `/tmp/c51-clean/venv` | 全 absent（`clean-env-build.txt` 步 4/5：`pip list` 里 akshare/openbb 0 行） | **151 passed / 3216 deselected、exit=0**（real 9.00 s）；`pip check` 无破洞 |

两段的 151 条**逐条用例名都在正文里**（各 151 行 `tests/...::... PASSED`），A/B 的分子分母逐字相同 ⇒ 「干净环境」这一段不是少跑了东西。探针从此档案读 `CLEAN_RUN_EXIT`、`absent` 声明、以及档案点到的模块集合，并把「当前标记集有单元没进留档」判为红（`archive_blind`）——这是一条刻意的棘轮：**扩大标记集必须重跑干净环境证据**，否则正向证据会随分母膨胀而悄悄变成部分证据。

包数说明：`clean-env-build.txt` 步 5 的 `131` 是 `pip list` 行数（含 `pip`/`setuptools`），普查面 7 的 `129` 是按 import metadata 计数，两个数不是同一分母。

## 五、`AC-16|06` 为什么仍然 gap（不是"再读一遍"能推进的）

判据原文要清的是「集成层的 `import akshare` 残留」：**实测 import 形态 = 0、动态 import 形态 = 0**。基线里剩的 3 条不是依赖，是被扫描器按"纯模块路径字符串"规则抓到的**产品事实名字**（FR-7 对照表自己的字段名、`DataScript.source` 的溯源默认值、`key_status()` 的数据源标签）。把它们变成 0 只有两条路：

1. **把扫描器字符串规则改窄** —— 等于回头放宽已勾选的 `AC-16|05`「分层 AST 口径」，而那条的口径正是"名字面量也算"。本轮拒绝。
2. **改掉这三处拼写**（例如写成 `AKShare`）—— 内容不变、字面量变化，纯粹为了穿过门禁。这正是 C35 收口过的「门禁空转」形状：排除条件看着像守卫，实际谁都不咬。

⇒ 这一格现在是一条**用户/产品决策**（登记与 task #52 同列）：要么接受「基线 3 条名字面量长期存在」并改写判据措辞，要么由产品侧决定溯源字段的取值域命名。本轮做的是把这件事量化到可决策，并让"只降不升、新增即失败"两条门禁命令与 517 文件走查读数继续成立 —— 这两条命令的原样复跑单独入库为 `zero-dep-rerun-c51.txt`（`ZERO_DEP_CHECK_EXIT=0`、`OK: 517 file(s) walked …`、`frozen baseline: 3`），台账本格的 evidence 列指向它，而不是指向任何一遍门禁日志。

## 六、门禁与自测遍次

| 遍次 | 命令 | 读数 |
|------|------|------|
| 判定工具反事实遍（本机手工，19:44–19:48，`real 261.34 s`、exit=0） | `python scripts/quality/acceptance_item_probe.py --self-test` | 末尾两行逐字：`  - 反事实面：33/33 个探针走到了判定，260 条 break 各被施加一次、每条都要求把干净读数打回 gap` ／ `OK: 33 probe(s) measured; every judge is reachable from its declared repair, and all 260 counterfact(s) flip a clean reading back to a gap.`（C50 是 `31/31` 与 `243` 条 ⇒ 本轮 +17 = `AC-16\|06` 的 7 面 + `AC-16\|07` 的 10 面）。**这一遍没有单独入库**：它只有两行正文，而同一件事自 C50 起是门禁第 11 个成员 `acceptance-probe-check`（`--gate-check`）每遍重跑、并把同样两个分母印进门禁日志；引一份两行档案不如引那份完整正文 |
| 门禁遍 1（回填后、定稿前） | `make gate`（17 个成员 / 18 段 `===== gate:` 横幅，成员 11 = `acceptance-probe-check`） | 正文见同目录 `gate-run1.txt`（档案 7498 行 = 10 行溯源头 + 7488 行完整未裁剪正文，`GATE_EXIT=0` 读自档案自身末行）。逐字读数：`real 440.55`；成员 2 `OK: 517 file(s) walked …（frozen baseline: 3）`；成员 11 `本遍墙钟 = 266.7 s，读到事实的探针 33/33`、`VERDICT AC-16\|06: gap`、`VERDICT AC-16\|07: proven`（两格首次在同一次 measure 遍里被判到）；成员 12 `3278 passed, 6 skipped in 74.24s`、`TOTAL 11636 1086 2874 288 89.28%`（6 条 skip 仍全部归因 `tests/test_port_fidelity.py` 的 em 网络阻塞，`-rs` 可复核）。**这一遍不是定稿遍**：它跑时本 README 还没有 §五 末句与本表下面两行，所以它描述的是"回填后、定稿前"的树；`-m 'not e2e'` 全程生效，未触真库 |
| 零依赖两条命令的独立留档 | `make zero-dep-check`（`--self-test` + 默认核对，逐字取 `Makefile:119-120`） | 正文见同目录 `zero-dep-rerun-c51.txt`（20 行，`ZERO_DEP_CHECK_EXIT=0`、`real 1.07`）。单独入库的理由写在 §五 末句：`AC-16\|06` 的 reason 要引用这句话，而不能引用晚于它写成的门禁定稿遍 |
| 门禁遍 2（定稿文字遍） | `make gate`（同一棵代码树、同一 17 成员，跑在本 README 全文与台账、验收文档定稿之后） | 正文见同目录 `gate-run2-final.txt`（档案 7504 行 = 16 行溯源头 + 7488 行未裁剪正文，末行 `GATE_EXIT=0`，`real 434.43`）。它的代码面读数与遍 1 **逐字相同**（`TOTAL 11636 1086 2874 288 89.28%`、`3278 passed, 6 skipped`、`517 file(s) walked … frozen baseline: 3`、`33/33 探针` 与 `260 条 break`）⇒ 这就是「回填没有动代码」的证据本身；唯一按预期变化的是成员 5 的 `files census = 543 (gate logs: 94)` → `545 (95)`，因为本目录新增的两份档案进了普查面。**本行不引遍 2 自己的结论性读数**（它描述的是"含本 README 这段文字之后"的树，而它自己晚于这段文字） |
| 门禁遍 3（提交后的干净树复验遍） | `make gate`（三个 C51 提交落地、`git status --porcelain` = 0 之后） | 正文见同目录 `gate-run3-clean-tree.txt`（档案 7503 行 = 16 行溯源头 + 7487 行未裁剪正文，末行 `GATE_EXIT=0`，`real 437.54`）。**这一遍是本轮唯一在干净树上跑的门禁**，因此也是唯一真正判了 worktree/index/history 那一面的遍：成员 11 打 `工作树：干净`（前两遍打的是 `23/25 entry(ies)` ⇒ 记 deferred），台账↔读数 `agrees=33, unflipped=0, open=0, deferred=0, stale-proof=0`，且 `VERDICT AC-1\|10` 从前两遍的 gap（"residue, files git has never seen, uncommitted edits"）**翻成 proven** —— 那一格的判定面此前只是因为树脏而不判，不是数据变好；台账早就记 `proven`（C40），三遍合起来才第一次由门禁本身证实。其余读数与遍 2 逐字相同（`3278 passed, 6 skipped`、同一 `TOTAL` 行、`517 file(s) walked`、`33/33` 与 `260 条`、`files census = 546 (gate logs: 96)`） |
| 门禁遍 4（最终文字遍） | `make gate`（本 README §六 改写 + `gate-run3-clean-tree.txt` 入库 + 台账 `AC-1\|10` 补一条证据 + 验收文档 ⑧ 扩写与 §10 补两个路径之后） | 正文见同目录 `gate-run4-final.txt`（档案 7506 行 = 18 行溯源头 + 7488 行未裁剪正文，末行 `GATE_EXIT=0`、`real 441.26`）。代码面读数与遍 2／遍 3 逐字相同（同一 `TOTAL` 行、`3278 passed, 6 skipped`、`517 file(s) walked`、`33/33` 与 `260 条`）；成员 5 的 `files census = 547 (gate logs: 97)`。**这一遍的 `deferred=1` 是本轮最该被看见的自指**：`工作树：4 entry(ies)` ⇒ 探针把时刻面记为 `deferred（时刻面，等工作树干净再判）：AC-1\|10`，`agrees=32, unflipped=0, open=0, deferred=1, stale-proof=0`，那一格读回 gap。任何"描述最终文字"的门禁遍都必然跑在含未提交改动的树上，所以这一遍不能同时是干净树遍 |
| 门禁遍 5（含全部文字且工作树干净） | `git commit` 之后 `make gate` | 正文见同目录 `gate-run5-clean-final.txt`。**本行不引它的读数**：遍 5 的存在理由正是"树 = `4dad458`（遍 4 的 HEAD 是 `e4f85a1`，之后又落了 `3c4a25b`／`18f5e9b`／`4dad458` 三条）、工作树干净、且树里含上面每一段文字"，它自己那份档案只能写在它跑完之后 —— 要核对就打开正文读末行 `GATE_EXIT=`、成员 11 的 `工作树：干净` 与 `deferred=0`。本行那句 `4dad458` 也写在遍 5 启动之后（计数由『五个』更正为按 hash，见档案头『事后文字更正』）—— 描述某一遍的那句话永远晚于那一遍 |
| 回填前的单项复算 | `python scripts/quality/a2_check.py`（11.11 s，四行 `ok`：ruff check / ruff format / mypy / bandit）、`python scripts/quality/acceptance_ledger_check.py`（`OK: items=130 proven=32 gap=8 unreviewed=90 ticked=32`）、`make brand-check`（`OK: no brand residue…`）、`python scripts/quality/acceptance_item_probe.py --sync-faces`（`33 个探针，12 个在读 moment 面，proven 而无探针 7 个`，写回的 diff 恰为本轮那 +6 行） | 全部 exit=0。这些是「花钱跑门禁之前的便宜复算」，不替代门禁遍 |

五遍的分工：遍 1 判回填后的代码，遍 2 判定稿文字（与遍 1 的代码面读数逐字相同 ⇒ 回填没有动代码），遍 3 判提交后的干净树（第一次真判 worktree/index/history 面，`agrees=33 … deferred=0`，前两遍因树脏读作 gap 的 `AC-1\|10` 在这里读作 proven），遍 4 判"含本轮最后一段文字"的树（代价是时刻面重新 deferred），遍 5 把两者合上：提交之后再跑一遍干净树。⇒ 自指的尽头是没有哪一遍能同时"含全部文字"与"工作树干净"，除非它的档案晚于自己；遍 5 就是按这个办法做的最后一遍，它自己的档案是唯一晚于它的文件。

## 七、仪器自身缺陷与复跑披露（不粉饰）

1. **`census-before.txt` 由该仪器更早一版打印**：它的 5a 把「注册 marker」读成空串并把内建 marker 标成 `NOT registered`，面 4 把 `akshare` 那一行重复印了一次。两处都在 `census-after.txt` 之前修好，两遍因此不是同一份源码。本档案照原样留档而不回填。这条不影响本轮结论：决定性读数 `applied integration = 0`、`selected = 0` 与 `--venv` 遍的 7b 一致，且**独立于该仪器可复核** —— `git grep -l "pytest\.mark\.integration" HEAD -- tests/` 在 `d1f0c81` 上 0 命中，而 `HEAD:pytest.ini` 第 23 行就在注册它。
2. **`run_p0_integration_surface.sh` 第一版把 summary 段发到 stdout**（`grep ... "$OUT"` 漏了 `>> "$OUT"`），于是 19:13 那份档案以「===== summary line =====」空段结尾。修脚本后同一仪器在 19:34 重跑第二遍，两遍的用例数与 exit 完全一致（151 passed / exit 0 × 2）；分母差 5 例（3362→3367、3211→3216）只因本轮新增的 5 条门禁断言进了收集面，它们不带 `integration` 标记故仍被 deselect。入库的是第二遍。
3. **反事实遍次**：CF3 在第一版打不红断言 3（它读的是 allow-list 常量），暴露后把断言 3 改成读活面重跑 —— 见 §三 表格第三行。

## 八、复算指令

```bash
cd /Users/yunjinqi/Documents/new_projects/opendata
set -a; . ./.env; set +a                     # 门禁成员需要环境里的开关，全程不读取其值
export PYTHONPATH=.

# 判定面 + 四条反事实
python -m pytest tests/test_p0_integration_surface.py tests/test_acceptance_probe_gate.py --no-cov -q
# 两个解释器各一遍（B 段需要先按 build_clean_env.sh 重建 /tmp 下的 venv）
bash docs/evidence/C51/run_p0_integration_surface.sh
# 七面普查（只读）
python docs/evidence/C51/zero_dep_clean_env_census.py --venv /tmp/c51-clean/venv
# 条目级判定面
python scripts/quality/acceptance_item_probe.py --item 'AC-16|06' --item 'AC-16|07'
# 两本账对账 + 门禁
make ledger-check && make gate
```

## 九、安全面与已知边界

* **对真库零访问**：所有 pytest 遍都带 `-m "integration and not e2e" --no-cov`；应用经 httpx `ASGITransport`（不跑 lifespan），fixture 引擎是 sqlite 内存库；未执行任何写入或 DDL；未读取或打印任何密钥值与 `.env` 内容（venv 安装只读 `pyproject.toml`）。AC-15 旧表仍只读、未 DROP。
* **未改写任何历史轮次档案**；本轮 5 份档案 + 2 台仪器 + README 以显式 pathspec 提交，`git show --stat` 复核。
* **不要把本轮读成「本项目零上游依赖」**：本轮挣到的是「`AC-16|07` 那句话有 151 条具名用例作分母，且在依赖面恰为声明集的干净 venv 里它们全绿」。基线仍冻 3 条名字面量、`AC-16|06` 仍 gap、集成层残留的清零待用户决策。
* **判定面的已知边界**：`AC-16|07` 探针读的是本机时间窗内的一次留档，不是每次门禁重跑 venv（重跑要 30 s+ 与一次网络安装，且会把 `make gate` 变成依赖 pip 镜像的闸）。因此它保证的是「留档在树里、留档覆盖当前标记集、留档自证 exit」；若有人删档案或扩标记集不补证据，门禁当场红。
