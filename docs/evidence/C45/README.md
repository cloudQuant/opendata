# C45 — AC-17|03「A1 棘轮 + 触碰即达标」与 AC-17|05「搬运层仅 E/F + 与上游可 diff」：把两句「不高于快照」做成能红的面

本轮闭合 AC-17 条目级最后两格（`8/10 → 10/10`）。两格的判据原文各含两半，此前都只有前半有机器面：

| 条目 | 判据原文（回填前 line 264 / 266，回填后 265 / 267——§0 多了一行 v5.17） | 修前已有的面 | 修前没有的面 |
|------|--------------------------------------|--------------|--------------|
| AC-17\|03 | A1 层：mypy/ruff 债务不高于基线快照，**只降不升**；**修改过的文件按 A2 标准达标**（"触碰即达标"） | 棘轮比较工作区与快照（`ratchet.py`） | ①上限自身的历史（抬高 `ratchet.json` 里的数字是一次正常提交）；②触碰集 ↔ A2 集的对账；③三个平面的**可见性**（被 exclude 吞掉的根不参与任何一侧） |
| AC-17\|05 | 搬运代码：仅 E/F 检查、未做 format/isort 重排（与上游可 diff）；pre-commit 含 `exclude: opendata_http/`；lint 债务不高于棘轮快照 | 棘轮数 `ruff_ported` / `direct_http_ported` | ①「仅 E/F」这一约束本身（把 `select="E,F"` 改宽只会*抬高*快照，棘轮永不红）；②「与上游可 diff」的逐文件重放；③三处人工改动登记与 pre-commit exclude 的一致性 |

探针 `scripts/quality/acceptance_item_probe.py` 的两格现在各自给出可复算判定（`VERDICT AC-17|03: proven`、`VERDICT AC-17|05: proven`），并各配一套真实工作树上的反事实（8 条 + 7 条）。

---

## 一、先量：`alembic/` 与 `alembic_data/` 对三个静态面全隐形，而「触碰即达标」声称覆盖它们

`[tool.ruff].exclude` 里有 `"alembic/versions"`，`[tool.mypy].exclude` 里有 `^alembic/`、`^alembic_data/`。这不是"生成产物被合理排除"：`alembic_data/versions/*.py` 是**手写模块**（只有 SQL 文本由 `opendata/pipeline/ddl` 生成），`alembic/versions/0002-0003` 也是手写的。10 个一方模块因此既不产生读数也不产生红灯——被 exclude 吞掉的根在棘轮里是**双向静默**的，这正是"债务只降不升"最贵的一类漏洞。

修前 census（`alembic-before-census.txt`，在 `git worktree add /tmp/c45head da465b0` 解出的 HEAD 树上跑）：

```
14  E501  line-too-long        6  UP007  non-pep604-annotation-union
 3  D415  missing-terminal-punctuation
 2  I001  unsorted-imports     2  UP035  deprecated-import
 1  W291  trailing-whitespace                        Found 28 errors.
1 file would be reformatted, 9 files already formatted
mypy alembic alembic_data → exit 2: There are no .py[i] files in directory 'alembic'
bandit -c bandit.yaml -r alembic alembic_data → 0 findings
```

两条读数各自都是判据面的一部分：

- **只传目录会量到一个假零**。在 HEAD 树上 `ruff check alembic alembic_data` 回 `All checks passed!`——带斜杠的模式在目录 walk 中生效，必须逐文件显式传参才看得到那 28 条。这条语义直接决定了反事实 `ruff-exclude-nested` 与 `ruff-exclude-top-level-inert` 的分界（见第四节）。
- **`mypy` 整根被排除时目录展开成空集并以 exit 2 硬失败**。这就是反事实 `mypy-exclude-whole-root` 的形状：棘轮抛 `ToolError` 退出 1，探针判 `error` 而不是放行。

## 二、处置是改文件，不是改快照

28 条违例 + 1 个未格式化文件全部修掉；`ruff_selfdev` 因此从「HEAD 快照 243」冻到 242。这一格里有个必须写出来的事实：

```
$ cd /tmp/c45head && PYTHONPATH=. python scripts/quality/ratchet.py
OK: quality debt did not increase.
  ruff_selfdev: 242 (snapshot 243) (improved)
```

**上限 243 里含着 1 条已经不存在的债务**（某轮修掉之后没有 `--update` 复冻）。棘轮只会说 `improved`，永远不会说"上限比现实宽"——这就是"只降不升"的后半必须读**上限自身的历史**而不是只比较工作区的理由。本轮的 `243 → 242` 是同一次 `--force-update`（范围扩张必须显式冻）里的复冻，工作区读数 242 与冻结后的快照 242 一致。

改动构成也写清楚，不让"只动了 docstring"替 490 行差异背书：`2026_09_22-0001_initial_schema.py` 的 490 行 diff 里绝大部分是 **`ruff format` 的引号与折行重排**（单引号→双引号、长调用拆行）——触碰即达标要求被触碰的文件 format-clean，修后 `ruff format --check alembic alembic_data` 回 `10 files already formatted`；其余是模块 docstring（D 规则）与注解／导入形态（`Union[str, None]`→`str | None`、`typing.Sequence`→`collections.abc.Sequence`、I001）。

DDL 没有被改动，并且这一条是可复算的：三个被改的 revision 里 **357 条非 docstring 字符串字面量**与 HEAD 按 AST 多重集全等。引号与折行正是 AST 比较会归一化的那一层，所以这条读数证的恰恰是"只有排版动了、建表与建索引的文本没动"。

## 三、平面可见性普查：谁被排除必须点名

`measure_ac17_03` 对三个平面各走一遍并交叉（探针第三行读数）：

```
平面可见性：6 个自研根磁盘 415 个 .py，ruff 自己走到 415（看不见 0）；
mypy 目标 192/210，被它排除的 18 个里 18 个在棘轮点名的遗留区（opendata/data_fetch/）之内、
0 个是没人声明的 exclude（这 18 个仍全部被 ruff 读到）；
bandit 251/251，被它漏掉的 0 个（ruff 也看不见的 0）必须为 0
全体 tracked .py = 784，既不在任何被测根下、也不在 A2 集里的 0
```

关键设计：判定要求的是 `mypy_dark_outside_legacy == 0`，而**不是** `mypy_dark == 0`。`opendata/data_fetch/` 的 18 个文件确实不在 mypy 面内（质量规范 §4 的 A1 遗留区，本轮把它从"配置里的一行"升格为 `ratchet.MYPY_LEGACY_ZONE` 里的**声明**）。之所以必须点名：一条没被声明的 exclude 与一处被合理 grandfather 的遗留区在旧读数里长得一模一样，而只有前者能在**不改一行代码**的前提下把 `mypy_selfdev` 洗小。

同一格还钉住 `bandit_dark == "0"`（此前是 `bandit_dark_seen_by_ruff`，只查"bandit 漏掉的是不是 ruff 也看不见"，等于允许两个平面同时漏）。

## 四、三个工具的 exclude 语义互不相同（全部实测，不是查文档）

| 反事实 | 植入 | 棘轮 | 探针 | 为什么 |
|--------|------|------|------|--------|
| `ruff-exclude-nested` | `[tool.ruff].exclude += "opendata/pipeline/"` | exit 0（债务掉到 242 以下，棘轮不拦自我变好） | **gap**：`ruff 自己走到 388（看不见 27）` | 带斜杠的模式在 walk 中生效 |
| `ruff-exclude-top-level-inert` | `exclude += "alembic_data/"` | exit 0 | **proven**（如实登记为不咬） | 门禁按根把目录显式传给 ruff，ruff 不对自己显式收到的根套 exclude ⇒ 这一条惰性。普查量的是**走到的文件数**，不是配置文件里写了什么 |
| `bandit-exclude` | `bandit.yaml: exclude_dirs += "pipeline"` | exit 0 | **gap**：`bandit 221/251，被它漏掉的 30 个` | bandit 按**路径子串**在任意深度命中，连命令行显式传入的文件也照排 |
| `mypy-exclude-nested` | `[tool.mypy].exclude += "^opendata/pipeline/"` | exit 0 | **gap**：`mypy 目标 165/210 … 27 个是没人声明的 exclude` | mypy 用 `re.search`，`^` 锚定后是根前缀；exclude 在 CLI 目录展开时生效 |
| `mypy-exclude-whole-root` | `exclude += "^alembic_data/"` | **exit 1**：`mypy failed: There are no .py[i] files in directory 'alembic_data'` | **error** | 整根被排除 ⇒ 展开为空 ⇒ mypy exit 2，两个工具同时拒绝读数 |

另有两条针对"上限"本身：`ceiling-lowered`（把 `ratchet.json` 的 `ruff_selfdev` 改低一格）⇒ 棘轮 exit 1 且打印 `ruff_selfdev: 241 -> 242`；`debt-planted`（在自研文件植一条未使用 import）⇒ exit 1 且 `242 -> 244`。

## 五、上限历史与「触碰即达标」

```
上限自己的历史（9 次转换）：抬高存量上限的次数 0；近 2 次转换里消失的被测根 0，更早历史 1；
存量上限轨迹 ruff_selfdev 404→242 / mypy_selfdev 35→11 / bandit_selfdev 6→3
触碰即达标：自基线起改动 360 个 .py ↔ A2 集 360 个（a2-check exit 0），
其中按名字被排除出 A2 的 0 个、排除项里的自研代码 0 个
```

「触碰即达标」被量成**两个集合的双向对账**，而不是"A2 检查跑过了"：触碰 360 ↔ A2 360，且差集为 0。C45 本轮就吃过它自己的一口——新写的 `docs/evidence/C45/*.py` 一进工作树就把触碰集从 358 抬到 359/360，`a2-check` 随即因缺 `# nosec B404` 与未 format 而 exit 1。那是判据真的着了，不是误报。

## 六、搬运层：仅 E/F 是量得出豁免量的，「与上游可 diff」由逐文件重放钉住

```
「仅 E/F」豁免挡住的量：同一棵树按自研规则集跑是 9709 条、按 I 规则 230 条，E/F 只有 2144 条
「未做 format/isort 重排」：209 个文件是 ruff format 会重写的、230 条 import 顺序违例
重放对账：exit 0，锁内 315 条记录 / 报告 315 行 / 逐字一致 315 行 / 待办 0 条；
  磁盘 313 个 .py，其中未登记的 0 个；现场渲染与已归档的 docs/port-report.md 逐字节一致 = yes
人工改动三处登记：upstream.lock 9 / THIRD_PARTY_NOTICES.md 9 / port_module.py:MANUAL_EDITS 9，不一致 0
.pre-commit-config.yaml：共 15 个 hook（HEAD 15 个），其中排除搬运树的 4 个，比 HEAD 少的 0 个
```

「仅 E/F」必须被**量出豁免量**（9709 vs 2144）而不是被写成一句配置说明：这条豁免是搬运层与上游可 diff 的前提，它挡住的 7,565 条违例如果看不见，就无法判断它哪天被悄悄取消或加宽。

`direct_http_ported` 的动词集合是本轮新加的第六面（`verbs_ok`）：两处读数都调用同一个 `count_direct_http`，行为对照**看不出** `HTTP_VERBS` 被改窄，而少一个 `request` 就会在不改任何搬运代码的情况下把 `direct_http_ported` 降下来 ⇒ 只能核对声明的集合完整性。

搬运上限历史如实打印而不并入判定：`抬高 3 次、其中搬运文件数未变的 1 次`（`3757285->d66dd99 ruff_ported 1305->2145` 就是 C44 平铺普查那一次）。判据原文只要求"债务不高于棘轮快照"，抬高因此登记为**待复核**而不是自动放行，也不被本轮的勾掩盖。

## 七、判据面自己必须能红：15 条反事实 + 139 条探针内建反事实

- `ratchet_counterfactuals.py` → `VERDICT counterfactuals=8 failed=0 exit=0`（八条逐条打印棘轮 exit、探针判定与 sha256 还原结果；见 `ratchet-counterfactuals.txt`）。
- `port_counterfactuals.py` → `VERDICT counterfactuals=7 failed=0 exit=0`（`port-counterfactuals.txt`）。七条分别是 `select-widened`（棘轮 exit 1、`ruff_ported: 2144 -> 9709`）、`verbs-narrowed`、`hand-edit-unregistered`（重放 exit 1、待办 1 条、逐字一致 314 行）、`format-applied`、`register-drift`（三处登记不一致 1 条）、`precommit-exclude-dropped`（比 HEAD 少 1 条排除）、以及正控制 `baseline`。
- `acceptance_item_probe.py --self-test` → `20 probe(s) measured … all 139 counterfact(s) flip a clean reading back to a gap`。

两条如实登记的**不咬面**（写在这里而不是藏进通过率）：

1. `ruff-exclude-top-level-inert` 判 `proven` —— 见第四节，根级 ruff exclude 在"按根显式传参"的门禁下是惰性的，这条路径不存在可红的读数；
2. `format-applied` 里树级的「未做 format 重排」读数**不红**（209 个文件 → 208 个，仍然 >0），红的是这一个文件与上游的逐行 diff（`逐字一致 314 行`、`待办 1 条`、`report_exit 1`）。也就是说：单文件被重排由重放面抓，整棵树被重排由 format 面抓，两侧不是同一个判据。

## 八、开发者视图与门禁面同源（Makefile 的一项收口）

`make typecheck` 此前只跑 `mypy opendata/`、`make security` 只跑 `bandit -r opendata scripts`，而棘轮的三个平面是 6/4/5 个根——`opendata_fuyao/`、`alembic/`、`alembic_data/` 里的违例只出现在门禁读数里，任何 `make` 目标都看不见。本轮把三项目标钉到同一批根（`PY_SELFDEV` / `PY_MYPY` / `PY_BANDIT`），并逐项复算：

```
ruff check  (6 根)            → Found 242 errors.     == ruff_selfdev 242
mypy        (4 根)            → 11 errors, checked 192 source files == mypy_selfdev 11 / mypy 目标 192
bandit      (5 根)            → 3 findings（auth.py:40 B105、auth.py:209 B106、constants.py:8 B105）== bandit_selfdev 3
ruff format --check (6 根)    → 5 files would be reformatted
```

那 5 个未 format 的文件（`opendata/api/metrics.py`、`opendata/api/scripts.py`、`tests/test_api_tasks_lifecycle.py`、`tests/test_main_direct.py`、`tests/test_tasks_direct.py`）都是**未被触碰的 A1 存量**，按 §2.3「A1 存量文件在被触碰前不强制重排」处理，本轮不并入任何判据——登记在此是为了让它可数，而不是让它变绿。

`CODE_QUALITY.md` 随动更正三处（§1 的 A1/A2/D 三行、§2.1 的 exclude 片段、§7 的平面与豁免声明规则）：旧文档写着 `exclude = ["akshare", …, "alembic/versions", …]`，与实际配置已不一致，且 D 行把 alembic 迁移归类为"生成产物"。

## 九、这一格现在证明什么、不证明什么

**证明**：A1 三项存量债务各有一份读数且 ≤ 上限；上限自身的历史只降不升（9 次转换 0 次抬高）；三个平面各自走到多少文件、谁被排除、被排除的是不是点名的遗留区；触碰集与 A2 集一一对应且 a2-check 绿；搬运树只按 E/F 计量、豁免挡住的量可数、与上游逐文件重放一致、三处登记一致、pre-commit 的 exclude 没少。

**不证明**：
- 不证明存量债务已经收敛——242/11/3 只是**没有变多**，其中 1 条（`ruff_selfdev` 的 243→242）本轮之前一直宽于现实；
- 不证明 mypy 面覆盖 `opendata/data_fetch/` 的 18 个文件（点名的 A1 遗留区，仍 dark，只是从此可数）；
- 不证明 `direct_http_ported` 的 1044 处直连会下降（它是渐进收口指标，本轮只钉住它的动词集合完整）；
- 搬运层的历史抬高（含 `d66dd99` 那次 1305→2145）仍是**待复核登记**，本轮没有逐条归因；
- 探针不是门禁成员。`make gate` 的红绿灯由 `a2-check` + `quality-ratchet` 承担，探针产出台账输入。

## 十、复算

```bash
export PATH="$HOME/opt/anaconda3/envs/py313/bin:$PATH"; export PYTHONPATH=.
python scripts/quality/acceptance_item_probe.py --item 'AC-17|03'   # → proven
python scripts/quality/acceptance_item_probe.py --item 'AC-17|05'   # → proven
python scripts/quality/acceptance_item_probe.py --self-test         # → 20 探针 / 139 反事实
python docs/evidence/C45/ratchet_counterfactuals.py                 # → 8/8，exit 0（临时改工作树，跑完还原）
python docs/evidence/C45/port_counterfactuals.py                    # → 7/7，exit 0（同上）
make a2-check && make quality-ratchet
```

两条反事实脚本会**在真实工作树上**临时改配置/搬运文件，因此不能与 `make gate` 或彼此并发；每条跑完按 sha256 核对还原，日志末尾逐条打印 `还原=sha256✓`。

## 十一、门禁两遍分别证的是哪棵树

- `gate-run1-prebackfill.txt`：跑在代码/配置/证据脚本已就位、验收文档与台账**尚未回填**的树上。
- `gate-run2-postbackfill.txt`：跑在 §2 勾选、§10 台账行、banner 全部回填之后，证明回填没有把任何一格判成更差。

两遍都在 `docs/evidence/` 之外暂存、运行返回后落档（C43 立的流程：门禁日志不可能在它自己运行的那一刻已被 git 跟踪）。第一遍曾按旧顺序停在自己的新档案上——`evidence-traceability` 把未跟踪的 `docs/evidence/C45/*.py` 与缺失的 README 判成 3 条 NEW VIOLATION（`GATE_EXIT=2`），那一遍的原始输出留在 `/tmp` 未入档，处置是补齐档案并入库后复跑，而不是放宽判据。

**run 1 的读数（读自档案文件内部，不是运行台）**：`wc -l` = 7,115 行、17 段 `===== gate:`、`===== gate: PASSED =====` 在第 7111 行、`GATE_EXIT=0` 在第 7114 行；`3105 passed, 6 skipped in 74.76s`、`TOTAL 11097 1078 2738 286 88.80%`、`A2 files: 360`、public-api `640/640` 双 100%、`quality-ratchet` 打印 `ruff_selfdev: 242 (snapshot 242)` 与 `ruff_ported: 2144 (snapshot 2144)`，`ledger-check items=130 proven=21 gap=7 unreviewed=102 ticked=21`＝回填前那一刻的树。落档后与暂存件 `cmp` 逐字节相同。

**run 2 的读数**：`wc -l` = 7,121 行、17 段、PASSED 在第 7117 行、`GATE_EXIT=0` 在第 7120 行；`3105 passed, 6 skipped in 77.39s`、`TOTAL … 88.80%`（与 run 1 同分母同分子）、`A2 files: 360`、public-api `640/640`，`ledger-check items=130 proven=23 gap=7 unreviewed=100 ticked=23`＝回填后的树。两遍之间**只有 markdown 与台账 json 在动**，判定性的读数（三项存量债务、搬运债务、覆盖率、A2 集、public-api 面）逐项相同，差的只是台账计数与耗时。

`gate-run3-doc-members.txt`：run 2 之后又改了两处 markdown 字句（本 README §二 把 alembic 的 490 行 diff 说成"只有 docstring 与注解"，实际绝大部分是 `ruff format` 重排；验收文档同一句随动），因此按 C43 的先例不重跑整条门禁，而是**逐个复算以 markdown 为输入的四个成员**（`ledger-check`／`brand-check`／`evidence-traceability`／`secret-check`），并列出 run 2 之后确实没有代码面改动。

## 十二、本目录档案清单

| 档案 | 内容 | 判据面 |
|------|------|--------|
| `README.md` | 本轮 §一–§十三 全量叙述 | — |
| `alembic-before-census.txt` | 修前 HEAD detached 树上 10 个 alembic 文件的 ruff/format/mypy/bandit 四读（含"只传目录会量到一个假零"的逐文件传参理由）＋ HEAD 棘轮读数 | \|03 反证 ① |
| `ratchet_counterfactuals.py` / `ratchet-counterfactuals.txt` | 8 条反事实（正控制／上限改低／植入债务／三个平面的 exclude 形状／整根 exclude／显式登记的惰性形状） | \|03 |
| `port_counterfactuals.py` / `port-counterfactuals.txt` | 7 条反事实（正控制／`--select` 加宽／动词集合改窄／未登记手改／真跑 `ruff format`／三处登记漂移／pre-commit exclude 少一条） | \|05 |
| `probe-ac17-03.txt` / `probe-ac17-05.txt` | 两个条目级判定面的完整读数（`VERDICT … proven`） | \|03 \|05 |
| `self-test.txt` | 探针自检：20 探针 / 139 反事实，每个 judge 都要被至少一条反事实扳红 | 全表 |
| `gate-run1-prebackfill.txt` | 回填前门禁全量日志（7,115 行，`GATE_EXIT=0`） | 树 |
| `gate-run2-postbackfill.txt` | 回填后门禁全量日志（7,121 行，`GATE_EXIT=0`） | 树 |
| `gate-run3-doc-members.txt` | run 2 之后只有 markdown 在动，故逐个复算以文档／档案为输入的四个成员，并附"无代码面改动"的 mtime 证据与本档案入库后的追溯复算 | 树 |

## 十三、后续登记（本轮不做）

1. `A2_SCOPE` 里的 `opendata_providers` 在本仓不存在，缺失目录被静默跳过；固定目录之外的 46 个 A2 自研文件目前靠探针加宽面复量（C43 登记）。
2. 搬运上限历史 3 次抬高、其中 1 次搬运文件数未变（`d66dd99` 的 1305→2145）需要一次专门归因，形如 C44 对平铺普查做的那样。
3. 未触碰 A1 存量的 5 个未 format 文件 + `ruff_selfdev` 242 条：按"触碰即达标"收敛，不设日程。
4. 平面普查只查"走到多少个文件"，不查"参数是什么"：`--select`/`-c bandit.yaml` 之类由 `tools` 版本指纹与反事实 `select-widened` 钉，仍不覆盖"改成别的规则集组合"的全部形态。
