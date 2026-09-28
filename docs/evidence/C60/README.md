# C60 —— 「把 UTC 换成 timezone.utc」不是收口：它把六个 A1 文件送进了零容忍平面，而那正是 C54 交还它们的原因

## 1. 入口：登记的任务，和修完那一刻才露出来的第二件事

登记的原话是清掉 C54 原样交还的 6 处 A1 债 —— 5 处 `from datetime import UTC`
（`api/tasks.py`、`api/auth.py`、`services/{execution,notification,retry}_service.py`）加
`services/scheduler_service.py:195` 的未标注参数，把 A1 mypy 平面从 6 错打到 0，并冻结棘轮。

照着做完那一刻，`make a2-check` **红了**：这六个文件因为「被改动」进入 A2 零容忍平面，
带着 **68 条 ruff + 2 条 bandit**（`bandit_selfdev 3`）。C54 README 第 228–235 行写的就是这个代价 ——
「把它们清干净要一并吃下 69 条 ruff」（69 = 68 + ANN001，第一条已被 mypy 的修法顺手吃掉），
并且明确说其中 4 条落在认证路径上需要判断。**所以那 6 处当时不是「忘了修」，是「不在本轮的可见面上」。**

⇒ C60 实际是两段：
1. **第一段**：A1 mypy 6→0（档案 `datetime-utc-cleanup.txt`，仪器 `run_c60.py`）。
2. **第二段**：触碰即达标 —— 六个文件收进 A2 标准，全平面重新转绿（档案 `a2-touch-comply.txt`，仪器 `run_a2_comply.py`）。

只做第一段会把门禁留在红上；这也解释了为什么这类债能一直躺着。

## 2. 第一段：为什么是 `timezone.utc`，而不是改配置

`datetime.UTC` 是 3.11+ 才有；`pyproject.toml:264` 的 `python_version = "3.10"` 让 mypy 按 3.10 判定，
于是报 `Module "datetime" has no attribute "UTC" [attr-defined]`。**这条配置本身就是判据**，不是 bug。

- 反事实 B3 把 `python_version` 换成 `"3.13"`：mypy 立刻 `Success: no issues found in 184 source files`
  （正文第 378 行）。**零代码改动的绿** —— 这就是本轮规则禁止改配置的理由，写在档案头部而不是藏在心里。
- 12 处 `datetime.now(UTC)` → `datetime.now(timezone.utc)`；`timezone.utc is UTC` 在 3.11+ 是同一对象 ⇒ 零行为变化
  （C54 档案已证，本轮不复述）。
- `_job_to_dict(self, job)` → `job: Any`：`ANN001` 已启用、`ANN401` **没有**启用，所以 `Any` 标注不需要 noqa。
  返回类型仍写裸 `dict` —— 本轮只清 mypy 报的那一条，不顺手把返回类型也改掉。
- 本轮自己引入的代价：`timezone.utc` 比 `UTC` 长 7 个字符，两行越过 100 列 ⇒ `E501 1→3`、
  `ruff_selfdev 219→220`（棘轮当场 FAIL），由 `ruff format` 收线吃回 218。「修 lint 引入 lint」如实登记。

## 3. 第二段：触碰即达标的实际面

六个文件修完 mypy 后仍有 68 条 ruff：`D415 31 / D212 27 / RUF100 3 / D107 2 / E501·F401·S105·S106·SIM114 各 1`。

- **63 条机械修**（`ruff check --fix --unsafe-fixes`）：D212 把摘要收上第一行；D415 补行末句号
  —— 这些是中文 docstring，D415 只认 ASCII `.?!`，所以补的是 `.`，与仓内既有拼写一致
  （如 `data_fetch/providers/akshare_provider.py:209` 的 `"""根据DataFrame自动创建表."""`）；
  RUF100 摘掉三条 `# noqa: ANN401`（规则没启用，那些 noqa 自己是命中项）；F401 删 `api/auth.py` 的死 `import os`；
  SIM114 折叠两条同臂 `if`。
- **7 条手改**：两个 `__init__` 补 docstring（D107）；`retry_service.py` 一处 f-string 拆行（E501，
  日志文本逐字符不变）；SIM114 折叠后触发 SIM102+E501，收成 `if settings.is_production and (A or B)` ——
  `or` 短路 ⇒ `password_changed_at is None` 时仍然不算第二次 bcrypt，语义等价；S105/S106 两条抑制。
- **两条抑制的形状**照仓内既有先例（`opendata/core/security.py:152` 的 `# noqa: S107  # nosec B107`）：
  `DEFAULT_PASSWORD = "admin123"` 不是凭证，是「账号还在用出厂默认口令」的**探测器**；
  `token_type="refresh"` 是 JWT 类别判别子，不是密钥。**但 B2 反事实证明这两条抑制是承重的**，
  而它盖住的另一个真问题在 §5，本轮没修。

## 4. 读数（两段相加后的终局）

| 面 | 修前 | 修后 | 出处 |
| --- | --- | --- | --- |
| A1 mypy（`opendata/` + `opendata_fuyao/`，184 文件） | 6 错 | `Success: no issues found in 184 source files` | 第一段正文 240 / 第二段正文 751（终局再读 7065） |
| `ruff_selfdev` | 219 | 150 | 棘轮冻结读数，第二段正文 1168–1172 |
| `mypy_selfdev` | 6 | 0 | 同上 |
| `bandit_selfdev` | 3 | 1 | 同上 |
| A2 零容忍平面 | `ok`（六个文件不在面内） | `A2 files: 398`，四项全 `ok` | 第二段正文 756、四项 `ok` 1156–1159、rc=0 于 1163 |
| 受影响用例（`-m "not e2e"`） | — | `611 passed, 2893 deselected`（67.86 s） | 第二段正文 1251 |

差值可对账：`ruff_selfdev` 219 → 218（第一段吃掉 ANN001）→ 150（第二段吃掉六个文件余下 68 条：218 − 150 = 68，与第一段正文的 68 条逐规则计数对上）；
A2 平面文件数从 390（上一轮门禁遍：`docs/evidence/C59/gate-run1.txt` 第 163 行） 涨到 398，多出来的正是「被本轮改动」的文件 + 仪器本身。

## 5. 顺手发现的真缺陷：默认口令探测器比对的是另一个值（本轮不修，如实登记）

- `api/auth.py` 用 `verify_password(DEFAULT_PASSWORD, …)` 判断「这个账号还在用出厂默认口令」，字面量是 `admin123`。
- `core/database.py:167` 引导 admin 时用的是 `os.getenv("ADMIN_DEFAULT_PASSWORD")`，没设就 `secrets.token_urlsafe(16)` **随机生成**。
- `QUICKSTART.md:68` 与 `DEVELOPER_GUIDE.md:43` 却把 `admin / admin123` 写成文档默认口令。

三处事实不一致 ⇒ 用 `ADMIN_DEFAULT_PASSWORD` 起栈的部署里，**从未改过口令的 admin 不会被要求改口令**
（比对的值从来不是它实际被创建时用的那个）。修法要么让探测器读同一个 env，要么和引导逻辑统一 ——
两者都动认证路径与文档口径，不在本轮顺手做；`F401` 那个死 `import os` 恰恰是这个 env 读取曾经存在过的残留。

## 6. 判据不变（本轮被禁止的捷径，逐条有对应反事实）

1. 不改 `python_version` —— B3（第一段）证明它能洗出同一条绿。
2. 不加 `# type: ignore`。
3. 不动 C45 的「A1 棘轮 + 触碰即达标」守卫断言。
4. 不给这六个文件加 `per-file-ignores` / 不缩 `a2-check` 的扫描面 —— 第二段的 B3 本来是冲这条路设计的，
   量出来的东西比「洗不洗得绿」更要紧，见 §7。

## 7. 仪器的缺陷也是本轮的账：A2 平面的入口是「改动」，撤销改动能离开平面

第二段的 B3 第一版假设是「还原 HEAD 内容 + 加 `per-file-ignores` ⇒ ruff 被洗绿、mypy 仍红」。
实测 **整个 A2 平面绿**，而且 `A2 files:` 少了一个（397）。查 `scripts/quality/a2_check.py` 的定义：
A2 集合 = 相对 baseline **改动过**的文件（含工作树改动）。这六个文件在 baseline..HEAD 之间从没被碰过
（它们是纯 A1 遗留），是本轮的改动把它们送进平面的；把文件还原成 HEAD 内容 ⇒ 工作树无改动 ⇒ **离开平面**。

⇒ 「撤销清理」是绕过零容忍平面的真实通道，平面自己看不见它。兜住这条路的是 **A1 棘轮**：
第二段修正后的 B3 同时读三个面 ——
ruff 在还原后的文件上 **仍红**（D415 复现，正文 5899–5900）；`a2-check` **全绿 rc=0**，可平面里只剩 397 个文件（正文 6231、6630–6633）；`scripts/quality/ratchet.py` **红**：`mypy_selfdev: 0 -> 1` 与 `ruff_selfdev: 150 -> 169`（正文 6638–6640）。
棘轮按全树计数，与「谁被改动」无关，所以 `mypy_selfdev` / `ruff_selfdev` 立刻回涨。
**结论不是「闸门够了」，而是：本轮把 150/0/1 冻进快照之后，撤销改动这件事的代价从「没人看见」变成「门禁红」。**

## 8. 台账面

`python scripts/quality/acceptance_item_probe.py --item "AC-17|03"` 在两段都冻完之后重算，判据原文两面都读到：
`ruff_selfdev 150(≤150)`、`mypy_selfdev 0(≤0)`、`bandit_selfdev 1(≤1)`，`scripts/quality/ratchet.py exit 0（face=none，printed=3/3）`；
「修改过的文件按 A2 标准达标」那一半读到 **触碰集 ↔ A2 集一一对应**：`自基线起改动 398 个 .py ↔ A2 集 398 个（a2-check exit 0），
其中按名字被排除出 A2 的 0 个、排除项里的自研代码 0 个`；上限自身历史 11 次转换里抬高存量上限 0 次，轨迹 `ruff_selfdev 404→150 / mypy_selfdev 35→0 / bandit_selfdev 6→1`；
三个静态平面的可见性各自点名：ruff 走到磁盘上 427 个 `.py`（看不见 0）、mypy 194/212（被排除的 18 个全在棘轮点名的 `opendata/data_fetch/` 遗留区内、0 个是无主 exclude，且这 18 个仍被 ruff 读到）、bandit 255/255（漏 0）；全体 tracked `.py` = 810，两边都不在的 0 个。
`VERDICT AC-17|03: proven`，台账现状 `state=proven，文档勾选=True` ⇒ **本轮不翻台账的一格，只把它的读数往低里推**（219/6/3 → 150/0/1）。
第 7 节那条事实边界要并进台账的读法：`proven` 靠的是棘轮与平面的合取，单报 `a2-check` 绿不构成清理完成的证明。

## 9. 本轮欠账（如实登记）

1. 13 个 `tests/*.py` 仍用 `from datetime import UTC`。tests 被 mypy 的 `^tests/` 排除、
   也被 ruff 的 per-file-ignores 摘掉 `ANN`/`D`/`S`，所以这是**风格债而非门禁债**；本轮没动它，
   因为动它不会让任何判据变绿，只会把 diff 面撑大。
2. §5 的默认口令不一致没修（要动认证路径 + 两份文档口径）。
3. 第二段的 diff 里有 108（+55 / −53，按本遍正文第 32–736 行的 diff 逐行数出来） 行是纯 docstring 规范化（D212 收上第一行 / D415 补句号）。
   它们行为无关但会进 blame —— 以后翻 `git blame` 看到这一轮，需要知道这批改动的性质。
4. `opendata/` 全树 `ruff_selfdev` 仍有 150 条；本轮只清了「被触碰的六个文件」那 68 条。
5. `make gate` 没有逐成员计时面，本段的墙钟只能按历史带宽说，不能归因到成员（见门禁档案）。

## 10. 档案表

| 文件 | 内容 | 证据档位 |
| --- | --- | --- |
| `datetime-utc-cleanup.txt` | 第一段：六个文件的 UTC/`job: Any` diff 原样、mypy 6→0、68 条余债的逐规则计数、棘轮 219→218 与 6→0 冻结、三条反事实（B1/B2 红、B3 演示改配置洗绿）、三对还原 sha256 | runner 自判 `C60_RUNNER_EXIT=0` + shell 独立 `RUNNER_EXIT=0`（正文 401–402 行） |
| `a2-touch-comply.txt` | 第二段：触碰即达标后的完整 diff（含 docstring 收线）、六文件 ruff/format/bandit 四项、全 A2 平面、611 条受影响用例、三条反事实（B1 D415 红 / B2 摘 nosec 红 / B3 平面离场与棘轮兜底） | runner 自判 + shell 独立转述 |
| `run_c60.py` / `run_a2_comply.py` | 两段仪器（可重复；会临时改写真实文件再按原文还原，跑前备份工作树、勿与门禁并行） | 源码，本身按 A2 标准扫过 |
| `gate-run1.txt` | 终局门禁遍（17 个成员，正文 7775 行一行未裁）跑在 `3cf0cf7 fix(lint)` 与 `5c94762 docs(evidence)` 之后的清洁树上：`GATE_EXIT=0`，墙钟 1969 s，八条预登记预测逐字命中（棘轮 150/0/1 三项等于快照且段内 0 次 `NOTE: run --update`、`A2 files: 398` 四项全 `ok`、`VERDICT AC-17\|03: proven`、单元面 `3414 passed, 6 skipped`、覆盖率 90.08% 过 84% 门槛、前端 e2e 18 passed） | 门禁遍次实测（两条独立退出码记录：档案正文 7774–7775 行；45 条头部引用机验 `VERDICT_CITATIONS=OK`）。**本行与本档案的头部是跑完之后写定的 post-run 编辑**，不改变被扫的树：写定后 `git status --porcelain` 复核只多出本目录（未跟踪），没有任何被门禁扫的文件动过 |
| README.md | 本文件：叙述面 | 叙事 |
