# C35 —— 四处"按名字排除"的静态门禁收口：`providers/akshare` 树重新可见（AC-17）

## 0. 本轮要收的账与结论

C34 之后仍然挂着的那句话是"看不见的面不是便宜，是没量过"。本轮就是去量一个具体嫌疑：
里程碑 A2 把仓根的 vendored 目录 `akshare/` 改名成了 `opendata_http/`，可是四处排除配置**还在按名字匹配**，
而它们的名字匹配是"路径里任何一段命中即可"——于是自家包 `opendata/data/providers/akshare/`（15 个模块）
跟着一起被判为"搬运层、不需要看"。

结论（全部实测在 `exclusion-before.txt` / `exclusion-after.txt`）：

1. **决策不是结论**：改名之后有 4 处按名字的排除配置、外加 1 处按名字的目录变量需要跟着改或删（§3 的表逐个列了）；
   一处都没跟上，因为没有任何一个面会报"我今天少看了 15 个文件"。ruff 的走查看不到 15 个文件，mypy 对故意的
   `int = "str"` 全树 0 行，bandit 连**显式传进去的文件**都不扫（反事实补测：它实际吞掉 **18 个**自家文件，
   见 §1 末尾），a2_check 在起进程之前就把候选全丢了。
2. **代价比想象的小，这正是它一直没被发现的原因**：该树重新可见之后的显性债只有 6 条 `I001` +
   5 个待格式化文件 + 0 条 mypy。如果有人拿"债只涨了 6 条"论证"排除无害"，那是拿结果反推：
   被排除期间的债不会进入任何计数，所以"没债"和"没看"在数字上长得一样。
3. **一处顺带量到的坏死**：`Makefile` 的 `PY_PORTED := akshare` 自 A2 起就指向不存在的目录，
   `make lint` 的第三条命令一直在失败。它不在 gate 里，所以坏了十几轮无人知晓（登记 §7）。
4. 四个面都收口为**按根目录段**匹配，判定逻辑与配置语义一致；新增 36 例守卫（`tests/test_a2_exclusion_scope.py`），
   每条都是双向的（撞名的包必须可见 **且** 根树必须仍被排除），并且 7 次变异全部把守卫打红（`MUTATION_FAILURES=0`）。

**回填落点只有一格**：本轮动的是 AC-17（工程质量门禁）。commit ① 标题里写的 `AC-2` 那一行如今是**契约层**
（Capability/Protocol/Registry），而 AC-16 是零依赖基线（本轮没加也没删任何依赖），两处都不该记这笔账 ⇒
v5.6 只回填 AC-17，标题与档案不一致的地方以这里为准。

## 1. 先量：这个面到底暗了多少（`exclusion-before.txt`）

拿 `ruff check <tree> --show-files` 当普查装置（它是"这次走查会看哪些文件"的权威输出，不是"有没有问题"）：

| 装置 | 读数 | 含义 |
| --- | --- | --- |
| `find opendata -name '*.py'` | 191 | 磁盘上的自家文件 |
| `ruff check opendata --show-files` | 176 | ruff 走查真正看到的 |
| 差集 | 15，**全部**在 `opendata/data/providers/akshare/` 下 | 无一条属于别处，不是巧合 |

另外三面的读法不同，因为它们各自的"排除"发生在不同层次：

- **mypy**：往 `models/stock_daily.py` 临时写一行 `int` 变量赋 `str`，`mypy opendata/` 全树视图提到该文件的行数 = **0**；
  把同一个文件**显式**交给 mypy 立刻报 `assignment` 错；把目录交给 mypy 得到
  `There are no .py[i] files in directory 'opendata/data/providers/akshare'`。⇒ mypy 的 exclude 是
  对路径做 `re.search`，`"akshare"` 这种不锚定的模式在嵌套路径上也会命中。
- **bandit**：见 §5 的两次变异。第一次标记不成立被保留在档案里；第二次（`import subprocess` → B404）
  先证明这行代码在仓库外能被报出来（对照组 `[('B404', 15)]`），再证明它在自家树里三面皆哑。
- **a2_check**：15 个候选文件里 `_is_a2_candidate` 判 `True` 的 **0 个**，当日 A2 集 297 个文件、命中该树 0 个。
  这是最省事的一面：它在调用任何工具**之前**就把文件丢了，所以连"工具为什么没意见"都不会被问。

**补测（`bandit-census-counterfactual.txt`）：bandit 这一面吞掉的不止 15 个。**
把裸名 `"akshare"` 加回一份临时配置（只写在 mktemp 里，仓库配置未动）跑同一条命令做反事实对照：
扫描文件数 **207 → 225**，新可见 18 个、反向差集 0。多出来的 3 个是**文件名里含 `akshare` 子串**的自家文件：
`opendata/data_fetch/providers/akshare_provider.py`（A1 遗留树）、`scripts/codemod/verify_no_akshare.py`、
`scripts/ops/akshare_fallback_cross_check.py`（后两个正是本轮棘轮变红的来源）。
⇒ 子串匹配比段匹配更宽：连"脚本名字里带 akshare"都能被排除掉。档案里同时写明这条读数的边界——
两次跑的是已修复的源码，所以 `findings 4 → 4` 只度量可见文件数，不能用来论证"没有 finding 被藏起来"；
被藏过的直接证据是棘轮 `4 → 6`。

## 2. 真值表：三个工具的排除语义是分别量的，不是记的

这一节是本轮的可复用产物。每条都在本轮实测（工具版本：ruff 0.15.20 / mypy 2.3.1 / bandit 1.9.4 / Python 3.13.5）。

**`[tool.ruff].exclude`**

| 写法 | 语义 |
| --- | --- |
| `"akshare"`（无斜杠） | 匹配**任意深度**的目录 basename ⇒ 撞名的嵌套包一起消失 |
| `"akshare/"` / `"./akshare"` / `"akshare/**"` | 只匹配走查起点的那一棵 ⇒ 这才是本轮想要的"根树" |
| `"/akshare"` | 什么都匹配不到（陷阱：看起来最"锚定"的写法其实最无效） |
| 相对性 | 模式相对于**走查起点**求值：`ruff check opendata_http` 会检查 309 个文件，`ruff check .` 一个都不看 |
| 显式文件参数 | 默认**绕过** exclude；要让它生效必须 `--force-exclude` |

**`[tool.ruff.lint.per-file-ignores]`**：相对项目根求值，没有 basename-at-depth 行为。
⇒ 原来那条 `"akshare/**" = ["E501"]` 自 A2 起就是死条目（目录已不存在），本轮删除并留下注释。

**`[tool.mypy].exclude`**：对路径做 `re.search`，只有 `^…/` 才是锚定；目录参数被过滤，**显式文件参数永远被检查**。
⇒ `"akshare"` 与 `"akshare/"` 在 mypy 这里都挡不住嵌套路径，必须写 `"^akshare/"` 形式。

**`bandit -c bandit.yaml` 的 `exclude_dirs`**：对路径做**子串**匹配，并且**对命令行显式传入的文件也生效**
（这与 ruff/mypy 都不同，是本轮最反直觉的一条）。
⇒ 子串匹配连**文件名**都能命中（实测 18 个自家文件被吞，§1 末尾）。规则收口为一句写进 `bandit.yaml` 注释：
**任何条目都不许是自家路径的子串**，并让 §4 的守卫去执行它。

## 3. 修复：四处配置各改各的，不搞统一抽象

| 位置 | 改法 |
| --- | --- |
| `pyproject.toml` `[tool.ruff].exclude` | 删裸名 `"akshare"`；`"opendata_http"` → `"opendata_http/"`、`"frontend"` → `"frontend/"`；逐条注释写明"无斜杠=任意深度"与 `"/akshare"` 无效 |
| `pyproject.toml` `[tool.mypy].exclude` | 单行改多行、全部锚定：`^opendata_http/ ^tests/ ^alembic/ ^alembic_data/ ^opendata/data_fetch/`（顺带把 C29 之后一直缺的 `^tests/` 补上） |
| `pyproject.toml` `per-file-ignores` | 删除死条目 `"akshare/**"`，注释记录实测的根相对语义 |
| `bandit.yaml` `exclude_dirs` | 删除 `- "akshare"`；注释写明"子串匹配 + 对显式文件也生效"，并给出条目准入规则 |
| `scripts/quality/a2_check.py` | `EXCLUDED_PARTS`（任意路径段）→ `EXCLUDED_ROOT_DIRS`（只看 `Path(name).parts[0]`）；补 `alembic_data`；模块 docstring 点名本轮教训 |
| `Makefile` | `PY_PORTED := akshare` → `opendata_http`，并注释这条命令自 A2 起一直在失败 |

一个"顺手"的取舍：`alembic_data` 是本轮才补进 a2_check 的。修复前它**并没有**被排除（`"alembic_data" != "alembic"`，
段匹配压根没命中），我一开始把它写成"应当被排除"的期望，守卫直接红了一条 —— 那是我的期望错了，不是工具错了，
新写法把它显式列出才和 mypy 的 `^alembic_data/` 对齐。这类"预期落空"记在 §7。

## 4. 守卫：量普查而不是量债（`tests/test_a2_exclusion_scope.py`，36 例）

一条原则：守卫若断言"某树有 N 条 lint 债"，下次清理干净就把守卫清掉了。所以断言的是**文件普查**与**匹配规则形状**。
六个类，每个都是双向的（撞名的包必须可见 ⇔ 根树必须仍被排除）：

- `TestTheCollidingPackageIsInvisibleToNoPlane`：先断言该树 ≥15 个文件（防空跑），再逐个断言
  `_is_a2_candidate` 为 `True`，并用 `resolve_files(None)` 走**真实 git 集**确认 15 个都在里面，
  外加"没有任何自家文件从 A2 集里掉出去"。
- `TestRootTreesStayExcluded`：`opendata_http/__init__.py`、`alembic/env.py`、`alembic_data/env.py` 必须是
  非候选（每个都先自检文件存在）；根走查 `ruff check . --show-files` 里不得出现搬运层文件，
  同时先断言 `tree_files("opendata") ⊆ 根走查结果`——否则"没看到搬运层"可能只是因为压根没看到东西。
- `TestRuffWalkCoversEveryFirstPartyFile`：`opendata`/`scripts`/`tests` 三棵树逐个普查相等，差集非空时把
  unseen/extra 打进断言消息；再加一条形状检查：exclude 里除白名单（`.venv`/`htmlcov`/`.git`/`__pycache__`/`node_modules`）
  外**每条都必须含 `/`**。
- `TestMypyExcludesOnlyWhatItSays`：把"全树视图里被排除的自家文件集合"量成恰好等于
  `opendata/data_fetch/` 子树，并要求 providers 树不在这个集合里；另 4 例正向确认根树确实被排除。
- `TestBanditExcludesNoFirstPartyName`：任何 `exclude_dirs` 条目都不得是任一自家路径的**子串**（列表本身断言非空），
  再用真实 bandit 运行证明搬运层确实被排除（否则"没有条目命中自家"也可以是"压根没在排除任何东西"）。
- `TestDeveloperViewTargetsExist`：从 `Makefile` 解析 `PY_SELFDEV`/`PY_PORTED`，逐个断言目录存在且含 `.py`。
  这一条专门用来拦"配置指向了不存在的树"——也就是本轮顺带发现的那个坏死。

## 5. 反证：7 次变异 + 1 次不成立的标记（`guard-mutation.txt`、`mutations.sh`、`bandit-exclusion-probe.txt`）

变异表（每次 `git checkout --` 还原并比对 sha256，收尾工作树只剩证据目录，`MUTATION_FAILURES=0`）：

| 变异 | 守卫反应 |
| --- | --- |
| M1 a2_check 退回"任意路径段命中"（缺陷本体） | 17 failed |
| M2 反向：把排除条件写成永不命中 | 3 failed（根树不再被排除必须被发现） |
| M3 `[tool.ruff].exclude` 退回修复前 | 3 failed |
| M4 `[tool.mypy].exclude` 退回修复前单行 | 2 failed |
| M5 `bandit.yaml` 重新加入裸名 `akshare` | 1 failed |
| M6 `Makefile` 的 `PY_PORTED` 退回不存在的目录 | 1 failed |
| M7 往新可见的树里写真缺陷（未用 import + 待格式化 + `int = "str"`） | `a2-check` 实跑 `A2_EXIT=1`，三条各自报出 |

M7 是"判定面真的有牙"的证据，不是守卫自证：它绕开守卫直接调 `a2_check --files <该文件>`。
`bandit-exclusion-probe.txt` 里保留了**第一次不成立的变异**：我原以为 `_C35_PROBE = "hunter2"` 会触发 B105，
实测连显式传文件都是 0 findings —— 查 bandit 源码后确认 B105 只对变量名含 password/pwd/secret/token/key 的赋值报警，
**这条变异本身就不是 finding**，因此不能据它断言 bandit 看不见该树。教训写进档案：
变异必须先证明"在别处会被发现"（第二轮的对照组干的就是这件事），否则它证明不了任何排除。

## 6. 债的处置：该修的修，棘轮红了不改快照

树重新可见后的显性债与去处：

- **ruff 6 条 `I001` + 5 个待格式化**：`ruff check --fix` + `ruff format` 落在 7 个文件上
  （`fund_etf_daily.py`、`index_daily.py` 是被 import 排序顺带改到的），修复后该树
  `All checks passed!` / `15 files already formatted`。
- **mypy 0 条新增**：全树 `mypy opendata/` 错误行数修复前后都是 21（该树贡献 0）。
- **bandit +2 → 棘轮变红**：`bandit_selfdev: 4 -> 6`。这里没有走"更新快照"这条路——快照一更新，
  两条新 finding 就永久合法化了。真正的成因是 `scripts/ops/akshare_fallback_cross_check.py` 的 B607（`git` 依赖 PATH）：
  按仓内既有惯例（`authority_reconcile_check.py` 就是 `# noqa: S603  # nosec B603 B607`）处理，
  用 `shutil.which("git")` 解析一次路径消掉 B607，B603 显式标注（argv 全为字面量、shell 关闭），
  并保留"git 不在 PATH 时返回空串"的降级。回到 4 条后棘轮恢复绿色，快照未动。
- 修复后的 ratchet 读数：`ruff_selfdev: 274 (snapshot 277) (improved)`、`mypy_selfdev: 21 (21)`、
  `bandit_selfdev: 4 (4)`、`ruff_ported: 2144 (2145)`、`direct_http_ported: 1044 (1045)`。
  **没有执行 `--update`**：两条 ported 的改善不由本轮产生（本轮 diff 里没有任何 `opendata_http/` 文件），
  把它们冻进快照等于给未来一轮冒领功（登记 §7）。

## 7. 登记（本轮没修的、量到的新洞）

1. **棘轮本身看不见名字式排除** —— 这是本轮最重要的结构性缺口。`scripts/quality/ratchet.py` 的
   `file_counts` 用 `rglob` 枚举，而 `count_ruff` 把**目录**交给 ruff；两套枚举不一致，
   所以"少扫了 15 个文件"不会体现为任何计数变化。本轮没有改它（改动=重冻快照=受控事件），
   只留下这条登记 + §4 的普查守卫。
2. 快照 `file_counts` 已陈旧：`opendata` 174→191、`scripts` 15→34、`tests` 139→160。同样是受控重冻事件，未做。
3. `make lint` 自 A2 起第三条命令一直失败，而 gate 不包含它 ⇒ 开发视图（`lint`/`typecheck`/`security`）
   在本仓的实际使用率约等于 0。本轮只把目标名改对，没有把这些面接进 gate。
4. `ruff_ported`/`direct_http_ported` 各 −1 的来源未归因（早于本轮）。
5. 沿用未动的债：000680 579 vs 578 未归因；eastmoney K 线连续 `NO_READING`（本轮 6 例 skip 即其夹具缺口）；
   akshare 10 条 `verified=False` 腿；C34 的 `index_constituent.as_of` 翻正判据**未被放宽也未动**。
6. 本轮两处预期落空（记录以免被当成"工具行为"）：`alembic_data/env.py` 修复前其实并不被排除；
   `ruff check opendata_http --show-files` 返回 309 而非 0（exclude 相对于走查起点求值）。守卫按实测事实写。
7. **两处 A2 集计数不能直接相减，差额未归因**：`exclusion-before.txt` 记的是 297（12:18，HEAD=38d85f5），
   而修复后档案与 gate 都是 307（HEAD=da327c0）。本轮收尾时把两版判定函数放在**同一份** diff 面上重跑
   （`a2-set-counterfactual.txt`），得到 292（旧段匹配）↔ 307（新根段匹配），差集恰为这 15 个文件——
   也就是说"排除造成的差额"是 +15，而档案里的 297 与今天重跑的 292 还差 5 个文件。这 5 个大概率是两次采样之间
   被删掉的临时 `.py`（本轮纪律要求"跑完即删 scratch"），但我**没有把它归因清楚**，所以 §8 只写 307
   并在这里留一条账，而不是把 297→307 讲成一个干净的前后对比。

## 8. 门禁（`gate.txt`，完整未裁剪 6591 行，含 `GATE_EXIT=0`）

| 阶段 | 读数 |
| --- | --- |
| brand-check / zero-dep / js-points | PASS |
| a2-check | A2 files **307**（见 §7 第 7 条：与修复前档案里的 297 不可直接相减），ruff/format/mypy/bandit 四项全 `ok` |
| quality-ratchet | OK，见 §6 |
| public-api-quality | 502 个公共可调用对象，docstring 100% / 注解 100% |
| test-cov | **2906 passed, 6 skipped**，51.14s（`-n 8 -m "not e2e"`）；coverage 10942 stmts / 1283 miss / 2692 branch / 303 partial = **86.75%**（门槛 84%） |
| frontend | lint / typecheck / collection（e2e 声明面采集守卫）PASS；vitest 全绿；playwright **18 passed**，判定面分类 18 ASSERTS / 18 LEAVES、named gaps 0、findings 0 |
| 收尾 | `===== gate: PASSED =====` / `GATE_EXIT=0` |

6 例 skip 全部在 `tests/test_port_fidelity.py`，跳过原因是 `recording pending` / `fixture pending`
（em 通道无读数，task #14 承载），**与本轮改动无关**，本轮没有为它们放宽任何断言。

## 9. 复算

```bash
cd /Users/yunjinqi/Documents/new_projects/opendata
export PATH="$HOME/opt/anaconda3/envs/py313/bin:$PATH"; export PYTHONPATH=.

# §1 普查装置：两个数字必须相等（当前都是 191）
find opendata -name '*.py' | wc -l
python -m ruff check opendata --show-files | wc -l

# §4 守卫（36 例）与 §5 反证（会临时改配置再还原，收尾要求工作树干净）
python -m pytest tests/test_a2_exclusion_scope.py -q
bash docs/evidence/C35/mutations.sh          # 期望最后一行 MUTATION_FAILURES=0

# §1 末尾的 bandit 反事实（当前配置下的扫描文件数，应为 225；旧语义那侧见档案）
python -m bandit -c bandit.yaml -r opendata scripts -f json \
  | sed -n '2,$p' \
  | python -c "import json,sys; d=json.load(sys.stdin); print('scanned=', len([k for k in d['metrics'] if k != '_totals']))"

# §7 第 7 条的 A2 集重放（期望 307 292 15）
python - <<'PY'
import importlib.util, pathlib
spec = importlib.util.spec_from_file_location("a2", "scripts/quality/a2_check.py")
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
files = m.resolve_files(None)
old = frozenset({"akshare", "opendata_http", "alembic", "frontend"})
kept = [f for f in files if not any(p in old for p in pathlib.Path(f).parts) and (m.REPO_ROOT / f).is_file()]
print(len(files), len(kept), len(set(files) - set(kept)))
PY

# §6 门禁面
python scripts/quality/a2_check.py && make quality-ratchet && make gate
```

## 10. 档案清单

| 文件 | 内容 |
| --- | --- |
| `exclusion-before.txt` | 修复前四处配置原文、ruff 普查 191/176 与 15 个差集文件、a2 候选 0/15、mypy 盲区变异（已还原、sha256 校验、`git diff --stat` 空） |
| `bandit-exclusion-probe.txt` | 不成立的第一次 B105 标记（保留）+ B404 变异：仓库外对照组 `('B404', 15)` vs 树内/显式/子树三面 0 |
| `bandit-census-counterfactual.txt` | 反事实对照：exclude_dirs 加回裸名 `akshare` 后扫描面 207 vs 225，新可见 18 个自家文件（含 3 个只因**文件名**含 akshare 子串而被吞的），并写明这条读数的边界 |
| `a2-set-counterfactual.txt` | 同一份 diff 面重放两版判定函数：307（新）↔ 292（旧），丢掉的就是那 15 个；同时记录 297 与 292 之间未归因的 5 个文件 |
| `mutations.sh` | 7 次变异的补丁/校验/还原脚本 |
| `guard-mutation.txt` | 基线 36 passed + 7 次变异的守卫反应 + M7 的 `A2_EXIT=1` + `MUTATION_FAILURES=0` |
| `exclusion-after.txt` | 修复后四处配置原文、普查 191/191、根走查仍 0 个搬运层文件、该树 lint/format/mypy 干净、A2 集 307 命中 15、bandit 225 scanned/4 findings、ratchet 读数 |
| `gate.txt` | 完整未裁剪 `make gate` 日志（含 provenance 头与 `GATE_EXIT=0`） |
