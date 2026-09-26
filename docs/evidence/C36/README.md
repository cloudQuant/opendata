# C36 — 验收文档「条目级判据」与 §10 台账对账（两本账合成一份守卫）

## 0. 本轮要解决的问题（不是"再补一个功能"）

迭代 1 验收文档此前有两本账：

- **§2–§6 的条目级判据**：`- [ ]` 形式的可勾选判据 **130 条**（分布在 22 个组：AC-1…AC-19 的条目块 + §4/§5/§6 三个平面）。本轮开跑前实测 **0 条被勾选**。
- **§10 的实现进度表**：19 行 AC 汇总，其中 **9 行的状态单元格写着"完成/建立/已通"**。

也就是说：*汇总账面声称达成，条目账面一条未达*，而仓库里没有任何机器能发现这个矛盾。这类矛盾正是本迭代一路抓出来的同一形状的问题（C23 权威表声明不存在的腿、C29 前端 18 例从未跑、C30 e2e skip 面、C33 provider 清单与注册表不符、C35 静态门禁静默排除整棵树）。

本轮口径（自缚，不可放宽）：**判据不得放宽；只有本轮实跑且归档通过的条目才允许勾选**。因此本轮的主要产出是"把矛盾量成判据并入库一个会咬人的守卫"，而不是把 §10 改绿。

## 1. 落地的守卫

| 产出 | 文件 | 作用 |
| --- | --- | --- |
| 条目级清单 | `docs/quality/acceptance-item-ledger.json` | 130 条条目 + 状态（`proven` / `gap` / `unreviewed`）。条目身份 = `组\|序号\|sha256(文本)[:8]`：**改写判据会把旧条目孤立成"清单里有、文档里没有"**，措辞漂移不能悄悄带走证据 |
| 对账器 | `scripts/quality/acceptance_ledger_check.py` | 双向对账：文档条目 ↔ 清单条目、勾选 ↔ `proven`、`proven` ↔ 命令/轮次/日期/证据路径存在性、`gap` ↔ 理由、§10 每行 ↔ `条目级 k/m` 实测值；声称完成而 k<m 必须带 `未逐条达标` |
| 门禁成员 | `Makefile`（`ledger-check`，gate 第 4 个成员） | 两本账漂移 = 门禁红 |
| 自检 | 同上 `--self-test` | 7 类漂移 + 缺证据路径 + 缺命令三类伪造全部被抓；`OK: ledger self-test passed` |

守卫设计取舍：它**不判"某条判据是否真的达标"**（那是人和证据的事），只判"账面是否自洽、声称是否有可查的落点"。因此它无法把假证据变成真证据，但能保证"0/10 却写完成"这种句子再也写不进去。

时序披露：`gate.txt` 里的 `ledger-check` 跑在本轮勾选**之前**（那时 `proven=2`）；AC-17 条目 1/2/4/9 勾选与条目 6 记 gap 之后的复跑完整留档在 `ledger-check-final.txt`（`proven=6 gap=4 unreviewed=120 ticked=6`，`--self-test` 同档）。本轮的账面改动只落在 `docs/` 与 `Makefile` 之外的文档/清单，Python 源码与 `gate.txt` 采集时逐字相同，所以不重跑门禁来"制造"一条新的全绿记录。

对账器第一次咬人咬的是本轮自己：勾完 4 条后它报 `AC-17 (line 370): discloses 条目级 0/10 while the document reads 4/10`，即"条目动了、汇总没动"——这正是它存在的理由。

## 2. 本轮挣到的条目（proven + 勾选）

| 条目 | 命令 | 证据 |
| --- | --- | --- |
| AC-16 条目 5 零依赖断言的扫描面 | `make zero-dep-check` | `zero-dep-surface-before.txt` / `zero-dep-surface-after.txt`（v1 的 scope 写了 `opendata_providers/`——全树**不存在**，且不存在的目录静默走查 0 文件；现普查 515 文件 = opendata 191 + opendata_http 313 + opendata_fuyao 9 + opendata_client 2，包集合/文件数/扫描器版本/解释器次版本入库） |
| AC-16 条目 9 凭证不在版本库（含全历史） | `make secret-check` | `gitleaks-before-history.log`（5 命中）→ `gitleaks-after-history.log`（0）+ `gitleaks-counterfactual.py/.log` |
| AC-17 条目 1 `make gate` 全绿且逐项显式阻断 | `make gate` | `gate.txt`（14 个成员标记 + `GATE_EXIT=0`，读自文件内部）+ `gate-run1-a2-lint-fail.txt`（成员 6 红 → `GATE_EXIT=2`，其后 8 个成员的标记**根本没出现**，即真的中止） |
| AC-17 条目 2 A2 层（新增/触碰文件） | `make a2-check`（`python scripts/quality/a2_check.py`） | 同一 `gate.txt`：`A2 files: 313` → ruff check / ruff format / mypy / bandit 四项 `ok`；run1 里同一集合 `FAIL ruff check / FAIL ruff format / FAIL bandit`，例外行带行级 `# noqa` + `# nosec` + 理由 |
| AC-17 条目 4 F821 不再被全局忽略 | 见 `f821-counterfactual.txt` 内命令 | 配置面 `F821` 不在任何 ignore/per-file-ignores 里 + **真放一个未定义名的探针文件**：ruff 报 `F821 Undefined name`，`RUFF_EXIT=1`，探针跑完即删 |
| AC-17 条目 9 前端 lint/typecheck/test 纳入门禁 | `make gate` 前端五项 | `gate.txt`：`frontend-lint`（0 errors / 33 warnings）、`frontend-typecheck`、`frontend-collection`（磁盘 14 文件 = 采集 14 文件）、`frontend-test`（106 passed）、`frontend-e2e`（verdict 判据 0 named gaps + playwright 18 passed） |

**记 gap 的条目（不勾）**：

- AC-17 条目 6 覆盖率：本轮实跑 `opendata/data` **91.98%**、`opendata_fuyao` **95.76%** 达标，但 `opendata/pipeline` **85.32%** 未达条目要求的 ≥90%（`coverage-per-module.txt`，最差：`notify.py` 51.81%、`factor_builder.py` 52.81%、`templates.py` 62.35%、`ods_writer.py` 63.58%、`watermark.py` 72.88%）。另外"A2 新代码 ≥85%"的分母本轮没有产生可核对的量（`a2_check` 不做覆盖率），故不声称。全局 86.78%（`gate.txt`，2912 passed / 6 skipped）只是背景数，不是判据。
- AC-16 条目 6/7、AC-1 条目 9：本轮前已登记的三条 gap，理由原样保留（基线未清零；缺"未安装 akshare/openbb 的干净环境 P0 集成测试通过"的正向证据；白名单形状豁免的代价）。

AC-17 其余 5 条（3/5/7/8/10）本轮**留在 `unreviewed`**，尽管今天的 gate 运行里有对应的绿成员。理由：`gate.txt` 只能证明"那个成员通过了"，不能证明"条目的整句判据被那个成员量过"。条目 3 需要棘轮"债务上升即红"的反事实（`ratchet.py` 无 `--self-test`）；条目 5 要查 pre-commit 的 `exclude` 与搬运树 lint 基线；条目 7 要先确认 `public_api.py` 的文件集合没有 C35 那种静默排除；条目 8 是抽审类判据，得人工抽样；条目 10 涉及历轮（部分早于本纪律）。把它们写成"看着像过了"就是本轮要消灭的病灶。

## 3. 反事实：允许面有没有牙

历史扫描从 `leaks found: 5` 变 0 是靠 `.gitleaks.toml` 的 SDMX dataflow-key 允许面。**允许面吞多了比红 CI 更糟**，所以本轮跑差分而不是看安静：

- `[0]` 本脚本自身（含分段拼接的合成凭证）过新配置：`exit=0 findings=[]`（探针不污染版本库扫描）。
- `[1]` 真实命中的 4 个文件复制体：旧配置 4 条命中 → 新配置 0 条、`exit=0`。**吞掉的正是这 4 条**。
- `[2]` 4 个合成凭证：旧配置 3 条 → 新配置同样 3 条、`exit=1`，**逐条相同**（无连带豁免）。
- `[3]` 真实 + 合成混放同一棵树：只剩合成的 3 条。
- `[4]` 已知边界：全大写带点的 4 段值旧报新不报 → 登记为豁免代价。

补充：早先一版反事实脚本凭"打码后的视图"重写命中行，只触发 5 条规则里的 2 条——那会让**更弱**的允许面看起来合格。改为直接复制真实文件后触发集合与历史一致。修 A2 违例后重跑，两次输出逐字节相同（`.log` 尾部补记）。

## 4. 已知边界（本轮实测出来的，不是猜的）

1. **形状豁免有代价**：允许面按形状而非语义放行，全大写带点的 4 段值会被放过（`[4]`）。
2. **gitleaks 版本漂移会改变命中数**：本机 8.30.1 全历史 5 命中，CI 钉的 8.21.2 报 3 命中。`secret_scan_check.py` 现按 `docs/quality/secret-scan.json` 钉版本，**版本不符即红、缺工具即红（不允许 skip）**。
3. **"全绿"隐含一个此前未登记的前提**：必须是 `pip install -e ".[web,dev]"` 的可复现环境。`curl_cffi` 无人声明时 CI 的 `test-cov` 死于 conftest import；现由 `tests/test_ported_import_closure.py` 在门禁内量成判据（含"阻断 module-scope 依赖必炸、阻断懒依赖不炸"的双向反事实）。
4. **CI 红还没全退**：本轮首次把 CI 侧事实入库（`ci-red-streak*.txt`）。五连红里只有两个 09-24 run 是 `leaks found: 3` + `ModuleNotFoundError: curl_cffi`（本轮闭掉的两面）；三个 09-23 run 死在 `tests/test_port_fidelity.py::test_ported_output_matches_upstream[financial_statement]` 的 float-repr 单元格（`'218472857114.40997' != '218472857114.41'`）与一个 `更新日期` 时区格，其中一次还叠加 `Total coverage: 83.70%` 未达 84% 地板。**据此本轮不声称 CI 全绿**，且 C5…C35 从未 push。
5. **单文件 pytest 需要 `--no-cov`**：`pytest.ini` 的 addopts 带 `--cov` 与 84% 地板，单独跑一个文件必然以覆盖率不达标退出 1，容易被误读成"测试挂了"。
6. `urllib3` 作为安全地板是**传递可达**证明的，不是直接 import 声明。

## 5. 复现

```bash
export PATH="$HOME/opt/anaconda3/envs/py313/bin:$PATH"; export PYTHONPATH=.
pip install -e ".[web,dev]"                       # 缺这步的"全绿"不算
make ledger-check                                  # 两本账对账
python scripts/quality/acceptance_ledger_check.py --self-test
python docs/evidence/C36/gitleaks-counterfactual.py
make zero-dep-check; make secret-check; make a2-check
python -m coverage report --include='opendata/pipeline/*' --no-skip-covered
make gate                                          # 完整未裁剪留档：gate.txt，GATE_EXIT 读自文件内部
```

## 6. 本轮没做的事

- 没有放宽任何判据文本；两条更正（AC-16 条目 5 / AC-17 条目 1）都是**加性**的，原文一字未删，用 `〔C36 加性更正…〕` 标注。
- 没有把 §10 的任何"完成"改成绿勾了事；19 行全部改为披露 `条目级 k/m`，9 行声称完成处带 `未逐条达标`。
- 没有 push；未动 AC-13 `dwd_*` 落库与 AC-15 DROP（等显式确认）。
