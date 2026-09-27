# C50 — AC-17\|01「逐项显式阻断」的判定面第一次在门禁里跑：46 份 gate 日志里真跑过判定面的 = 0

本轮把条目级验收判定工具 `scripts/quality/acceptance_item_probe.py` 做成 `make gate` 的第 17 个成员（`acceptance-probe-check`，跑 `--gate-check`），并先量清「成员化之前到底是什么形状」。量出来的三件事：**① 判定面从来没有在门禁里跑过**（46 份 gate 日志正文里真跑过判定面的 = 0；出现探针文件名的 31 份来自 `a2-check` 的文件清单，只证明文件存在）；**② 台账记 proven 的格子里有 3 格此刻判定面读出来是 gap**，而今天的 `--all` 对它们一律 exit 0；**③ 有 8 格 proven 干脆没有任何判定面**（含 AC-17\|01 自己）。AC-17 条目级维持 10/10、**一格未新翻**；本轮挣到的是「这 10/10 每一遍都被重新量一次」这件事本身。

## 〇、修前的真实形状（读自普查七面，全部只读）

| 判据点名的面 | 已有的面 | 没有的面 |
|------|------|------|
| 「每个门禁项都能让门禁中止」（AC-17\|01） | `gate:` 16 个成员、每项一行 `@$(MAKE) --no-print-directory`、前面一行 `@echo "===== gate: NAME ====="`、首个非零退出即中止 | **判定面自己不是成员**：命令里出现 `acceptance_item_probe.py` 的 target 数 = 0，`gate`/`quality`/`quality-full`/`pre-commit` 四个聚合 target 提到它的行数都是 0。AC-17\|01 在 C36 被判 proven 靠的是配方形状；形状之外那件事——「这一格的判据到底有没有被重新量」——从来没有门禁见证 |
| 「台账 proven 的格子仍然成立」（对账面） | `--all` 会打印每格读数、`--self-test` 会把每条反事实打回 gap | **两样都不在门禁里**：手工跑 `--self-test`（247.85 s）＋ `--all`（247.52 s）＝ 495.4 s，两遍各自 measure 一次；而且 `--all` 只在判据漂字时红。普查面 4 当场量到 3 格 stale：`AC-1\|10`、`AC-17\|03`、`AC-17\|10` 台账=proven、实测=gap，`--all` 对这三格 exit 0 |
| 「复算与留档可追溯」（AC-17\|07、\|10） | `evidence-traceability` 扫档案的五个面（narrative / date / identity / command / exit） | **它扫的是档案，不是判定**：判定面这一遍跑没跑、跑了几个探针、有没有格子从来没被 measure 过，都不在它的面里。⇒「档案齐全」与「判据仍然成立」是两件可以同时一件真一件假的陈述 |

四点结构性事实（不是措辞问题，是判定面问题）：

1. **「成员存在」与「判据被重新量」是两件事**。C49 把这句话用在别人身上（用例存在 ≠ 判据点名的那件事有断言），本轮它落在自己头上：一台判定工具挂在仓库里、被 `a2-check` 数进文件清单，但没有任何一遍绿色 gate 重新量过它的判定。
2. **不带红灯的度量是汇总，不是判定**。`--all` 打印 31 行读数然后 exit 0，正是普查面 4 读到的形状。要让它成为闸，必须把「台账 proven ⇒ 探针现在仍读 proven」这条不等式写进退出码。
3. **但这条不等式不能无条件成立**。普查面 5 量到 12/30 个探针的 measure 闭包会读 `git status` / 索引 / `git show REV:`，这三类面**随「这一遍跑在提交前还是提交后」而变**，而一个轮次按定义就是一棵「档案还在飞」的树；面 4 那三格 stale 全落在 12 个里面。若把「proven 必须仍读 proven」不分面钉死，每一轮在飞的遍次都会假红三次——假红的代价从来不是红，是**下一次有人把它改成 `\|\| echo`**。
4. **成本结论要由仪器自己量**。成员化的动机不是「多一道闸」而是「少一遍 measure」：一遍 251.7 s 同时喂反事实自检、漂字、对账、面基线、成员自证，替代 495.4 s 的两遍手工。这个数是逐个 measure 计时读出来的（面 3），不是估的。

## 一、先量：七面，跑在活树上（全程只读、不写、不碰真库）

`probe_gate_census.py`（`set -a; . ./.env; set +a; python docs/evidence/C50/probe_gate_census.py`）跑了四遍，定稿遍 `census-run2-final-readings.txt`（`command_exit=0`，正文 149 行，per-measure 251.7 s / 本遍 real 254.10 s）：

| 面 | 它量什么 | 读数 |
|------|------|------|
| 1 成员面 | `gate:` 启动哪些 target、有没有一个的命令行里有这台工具 | 成员 16、含探针的 target 0、四个聚合 target 提它的行数 0 |
| 2 手工遍次面 | 46 份 gate 日志里有多少真跑过判定面 | **0 / 46**；文件名出现过 31 次（全是 `a2-check` 清单）；含 `probe(s) measured` 判词的 round 8 个，全部是手工遍次 |
| 3 成本面 | 每个探针 measure 多少秒、spawn 几个子进程 | 30 探针合计 251.7 s、measure 循环墙钟 252.7 s、spawn 150（其中 pytest 108）；<1 s 的 11 个；最贵 `AC-18\|01` 24.9 s |
| 4 对账面 | 台账状态 vs 探针此刻的读数 | agrees 27 / **stale-proof 3**（`AC-1\|10`、`AC-17\|03`、`AC-17\|10`）；今天的 `--all` 对这三格一律 exit 0 |
| 5 状态依赖面 | 哪些探针的 measure 闭包读到 git 时刻 | 12 / 30 读 worktree/index/history，16 个只读树内容与 spawn 子进程；**面 4 那三格全在 12 个里** |
| 6 仪器依赖面 | 哪些档案仪器 import 了产品私有符号 | 80 个文件里 6 台、15 个私有符号；C49 已实测过一次形状搬家咬人（`_index` 换返回形状 ⇒ 门禁第 1 遍红在 C48 的普查件） |
| 7 覆盖面 | 台账格子与探针集合的对账 | proven 31 / gap 9 / 未审 90；**proven 而无判定面 8 格**、gap 而无判定面 2 格（`AC-16\|06`、`\|07`） |

读数遍次披露（不粉饰）：这台仪器自己跑四遍。run1（per-measure 253.2 s / real 255.57 s）有**三处仪器自己的读数缺陷**，单独留档 `census-run1-instrument-defects.txt`：① 把收尾的 `===== gate: PASSED =====` 数成了一个门禁成员（于是印出「成员 17 个」，真实 16）；② 成本那一行拿「本轮更早一次手工遍次的常数」去减本遍的 per-measure 合计，跨遍次相减必然可为负（当场印出 `差 = -5.7 s`）；③ 面 2 把「探针文件名在 gate 日志里出现过 31 / 46 次」当成判定面跑过的证据，而那 31 次全部来自 `a2-check` 的文件清单。run2（253.6 s）修完这三处但还没有覆盖面（面 7）；run3（251.5 s）把面 6/7 的打印顺序与编号印反；run4 才是定稿遍。四遍跑在同一棵树上，per-measure 散布 251.5–253.6 s 是计时噪声，成本结论（495.4 s → 一遍 251.7 s）与遍次无关。`manual-probe-timings.txt` 是成员化之前那两遍手工计时的原始读数。

## 二、改了什么（逐条对着判据）

| 面 | 改动 | 挣到哪一格 |
|------|------|------|
| 成员接线 | `Makefile`：新增 target `acceptance-probe-check` 跑 `--gate-check`（**不是 `--all`**：`--all` 恒 exit 0，把它接进门禁等于接一台只打印的仪器），并同步 `.PHONY`、`help` 的 A2 行、`gate:` 的横幅与 sub-make 各一行 ⇒ 门禁 17 个成员 / 18 段 `===== gate:` | AC-17\|01（判定面第一次每遍都被重量） |
| 一遍 measure 五读 | `acceptance_item_probe.py`：把 `self_test` 内联的循环拆成 `measured_readings` → `self_test_findings`，新增 `gate_check`：一次 measure 同时产出反事实、判据漂字、台账↔读数对账、面基线、成员自证，**五个面共用同一份 readings** | AC-17\|01、\|07 |
| 对账闸 | 新增 `reconcile` / `reconcile_state` / `stale_cell_finding`：`agrees / unflipped / open / deferred / stale-proof` 五个标签，**只有 stale-proof 是红灯**；worktree/index/history 面只在 `git status --porcelain --untracked-files=all` 为空时判，否则点名记为 deferred | AC-17\|01（红灯写进退出码，且不为难在飞的轮次） |
| 面基线 | 新增 `--sync-faces` 写 `docs/quality/acceptance-probe-faces.json`；`face_book` 用 **AST 调用闭包**从源码重算每个探针读哪些时刻面，`face_diff` 与已提交基线不符即红（要改必须跑 `--sync-faces`，让这件事成为有人看过的 diff）；`proven_without_plane` 只降不升（基线 7） | AC-17\|01、\|10 |
| AC-17\|01 自己的探针 | 新增探针（14 条反事实）：配方成员数、每项一个 sub-make、`-` 前缀、`\|\| echo` 遮罩、一行串两项、开发者视图混进门禁、`make -n` 数出的成员数与配方不符、环境 import 不在这棵树里、判定成员被移出 gate | 面 7 那个「proven 而无判定面」的集合 8 → 7 |
| 打印面 | `self_test_findings` 改为返回 `(findings, reading)`：门禁日志正文从此带上两个分母（走到判定的探针数 / 被施加的 break 条数）。此前「自检通过」与「什么都没量」在日志里长得一样 | AC-17\|10（读者不必再问「跑没跑」） |

未改的东西（刻意列出，避免被读成放水）：判据原文一字未动；**没有任何一格翻勾**（AC-17 十格在 C36–C45 已经 proven，本轮是给其中一格补上它一直缺的判定面，不是把它翻上去）；`--cov-fail-under`、`exclude_lines`、ruff/mypy 规则、其余 16 个成员一律未碰；`proven_without_plane` 那 7 格一格没减（减它只能靠新探针，本轮只减了 AC-17\|01 那一格）；`docs/quality/acceptance-item-ledger.json` 本轮一字未改。

## 三、判定面：每个断言都能被反证，且反证由机器执行

门禁日志从此打印 `VERDICT AC-N|NN: …` 逐格判词，一台只读文件名的仪器从此不能冒充判定面。四遍 `--gate-check` 加一组更早的「同一提交两遍」对照（同一棵 HEAD `7125165`，全部完整未裁剪留档；反事实 ① 那一遍的准备工作被自己的断言拦掉了一半，见下面第一条）：

| 遍次 | 树 | 读数 | 档案 |
|------|------|------|------|
| 绿遍（权威） | 9 entry 脏树 | `GATECHECK_EXIT=0`；反事实面 **31/31 个探针走到判定、243 条 break 各被施加一次**；agrees=27 / unflipped=0 / open=0 / **stale-proof=0** / deferred=4；面基线 12/31 在读 moment 面、proven 而无探针 7 = 基线 7；成员自证 = yes；real 251.54 s（measure 循环 250.4 s） | `gate-check-green-authoritative.txt` |
| 反事实 ①：把面基线挪走 | 只缺 `docs/quality/acceptance-probe-faces.json`，判定语义一字未动 | `GATECHECK_EXIT=1`，红在「基线缺失或形状不对：moment 面与无探针格子都没有可比的东西」；其余四面照常给读数（agrees=26、deferred=4、stale-proof=0、成员自证 = yes） | `gate-check-counterfact-no-baseline.txt` |
| 反事实 ②：把成员从 `gate:` 配方里删掉 | 只删那两行（横幅 + sub-make），target 本身留着 | `GATECHECK_EXIT=1` 两条：**stale-proof `AC-17\|01`**（「台账把 AC-17\|01 记成 proven，探针现在读出 gap（被读的面：文件内容，与这一刻无关）」）＋ 成员自证点名配方只启动 16 个成员而判定成员自己不在场；恢复后 Makefile 与备份 `cmp` 逐字节相同 | `gate-check-counterfact-member-stripped.txt` |
| 同一提交两遍（时刻面为什么必须 deferred） | 同一个 HEAD，三件档案未提交 vs 已提交 | 读数从 gap 翻成 proven —— deferral 规则的原始证据，不是本轮的推测 | `moment-pair-same-head.txt` |
| 绿遍第 1 次（缺分母打印面） | 9 entry 脏树 | `GATECHECK_EXIT=0`、墙钟 262.7 s、agrees=26 / deferred=5 / stale-proof=0 | `gate-check-run1-first-green.txt`（被权威绿遍取代，保留理由见档案抬头） |

两处如实记录的失败与一个残余面：

- **反事实 ① 那一遍本来想同时删成员，脚本自己把它拦下了**：那句「删掉含 `acceptance-probe-check` 的行」在五处命中（`.PHONY`、`help`、target 名、横幅、sub-make），而断言写的是 2 ⇒ 当场拦下、`Makefile` 一字未改，那一遍因此只量到面 ④（基线缺失），面 ⑤（成员自证）与 stale-proof 都没被那一次跑到。留档不删，重跑一遍把两件事分开量。
- **反事实 ① 里 `AC-17\|05` 从 deferred 变成 agrees**：同一棵 HEAD，`agrees` 26 → 27、deferred 5 → 4，差的那一格读的正是 `history(git log)` 与 `history(git show REV:)`，而那一遍的树少了一个未跟踪文件。这说明 deferral 规则**只挡假红，不挡「在飞的树上恰好读成 proven」**——所以成员资格必须由 AST 从源码重算（面基线），不能靠「这一遍跑起来红没红」。
- **残余面**：`--gate-check` 判的是「五面这一遍一致」，它不能证明时刻面在**干净树**上也一致。那一面要等一次提交后的复跑或 CI（本轮从未 push，见 §五-2）。

单元面：`tests/test_acceptance_probe_gate.py` 33 条（配方形状 6 / 成员自证会咬 2 / 面闭包 AST 走 6 / 对账规则表 5 / 面基线与棘轮 7 / CLI 模式 3，其中一条按 mode 参数化 / 反事实结构 2 / 反事实分母 2），3.62 s 全绿（`witness-pytest-gate-tests.txt`，`--no-cov` 定点跑，**不是门禁覆盖率读数**），且刻意**不跑任何一次真 measure**：一轮 measure 是 250 s，把它塞进单元测试等于把门禁时长搬进 `pytest`。它们钉的是「判定面的形状」，上面那几遍钉的是「判定面会红」。

判据**没有被放宽**的两处自证：

- 五面里最容易被做成假闸的是「proven 而无探针」的棘轮：它只准集合变小，变大的唯一出路是给那一格写 measure；把它写成「允许新增无面 proven」就等于把普查面 7 的读数重新藏起来。
- stale-proof 只在**稳定面**上红，这不是放水而是普查面 5 的直接后果：时刻面在脏树上改判为 deferred 并**点名列出**（权威绿遍：`AC-17\|03`、`AC-17\|08`、`AC-17\|10`、`AC-1\|10`），读者看得见是哪几格、为什么没判。

## 四、门禁与档案

| 遍次 | 档案 | 它验证了什么 / 为什么被取代 |
|------|------|------|
| 普查四遍 | `probe_gate_census.py`、`census-run1-instrument-defects.txt`、`census-run2-final-readings.txt` | 成员设计的全部依据；run1 的三处计数缺陷单独留档而不是删掉 |
| 手工两遍计时 | `manual-probe-timings.txt` | 495.4 s 这个成本结论的原始读数 |
| 同一提交两遍 | `moment-pair-same-head.txt` | deferral 规则的证据 |
| `--gate-check` 四遍 | `gate-check-run1-first-green.txt`、`gate-check-counterfact-no-baseline.txt`、`gate-check-counterfact-member-stripped.txt`、`gate-check-green-authoritative.txt` | 见 §三：一台闸不能只跑绿遍，绿遍也不能只有一句「五面全过」 |
| 单元见证 | `witness-pytest-gate-tests.txt` | 33 条的形状面，`--no-cov` 定点跑 |
| `make gate` 回填前第 1 遍（红，8 段） | `gate-run1-prebackfill-attempt-bandit.txt` | 4,034 行完整未裁剪、`GATE_EXIT=2`、real 57.63 s。红在本轮自己的普查仪器 `probe_gate_census.py:161` 的 bandit B607（字面量 `["git", *args]` 的部分路径）；`# nosec B607` 写在 162 行而 bandit 1.9.4 把这条 issue 报在 Call 节点的 161 行，抑制注释对不上上报行。新成员排在第 11 段，所以这一遍**根本没跑到它** |
| a2-check 单成员复验 | `a2-check-postfix-single-member.txt` | 400 行、`A2_EXIT=0`、`A2 files: 373` 四面 ok。处置是把可执行文件解析掉（`GIT = shutil.which("git")`，仓内先例：`evidence_traceability.py`、`a2_check.py`）而不是挪 nosec、更不动 bandit 配置；改完复验 `last_change()` 三条 commit 引用与 `census-run2-final-readings.txt` 逐字相同 ⇒ 动的是怎么起进程，不是读了什么 |
| `make gate` 回填前第 2 遍（绿，18 段） | `gate-run2-prebackfill.txt` | 7,494 行完整未裁剪、`GATE_EXIT=0`、real 421.15 s：判定面第一次真跑在门禁里（第 11 段自报 251.4 s ⇒ 约六成门禁墙钟），`31/31` 探针、`243` 条反事实、`agrees=30 / deferred=1 / stale-proof=0`、`3269 passed`（C49 是 3236，+33 即本轮新测试）。描述**回填前**那棵树：`验收文档.md` 还没有 v5.22 行 |
| `make gate` 回填后（18 段） | `gate-run3-postbackfill.txt` | 描述回填后的树：`ledger-check`／`brand-check`／`evidence-traceability`／`secret-check` 四个以 markdown 为输入的成员读到的就是最终那份文档；`GATE_EXIT` 读自档案内部，段数与逐成员读数以正文为准 |

四遍 `--gate-check`（含一遍准备工作被自己的断言拦掉一半的）+ 一组同提交对照 + 三遍完整 `make gate`（其中一遍红）+ 一次单成员复验，是这一轮实测出来的代价。每一遍都完整未裁剪留档，取代关系写在上面这一列而不是删掉旧的。**任何一遍都描述不了写下它自己的档案之后的那棵树**（C49 实测过的不动点）：把它写进档案就会再多出一个「跑完再写的档案」，所以这一句只写在这里和 commit message 里。

`gate-run2-prebackfill.txt` 与 17:53 那遍独立权威绿遍（`gate-check-green-authoritative.txt`）之间还第二次量到「读数随遍次时刻变」：同一 HEAD、生产码与测试码一字未动，只把本轮档案 `git add` 进索引，`AC-17|03`／`AC-17|08`／`AC-17|10` 三格就从 deferred 翻成 agrees（`agrees 27→30`、`deferred 4→1`），因为它们读的是 index 面（`git ls-files` / `ctx.tracked`）；仍 deferred 的 `AC-1|10` 读的是 `git status`（17 条在飞），判据原文要求「git 从未见过的文件 = 0」，它按判据就得等提交。**门禁里恒判的因此只有稳定面**，时刻面 deferred 并点名不是妥协，是被这两遍量出来的口径。

`acceptance_item_probe.py`、`probe_gate_census.py`、`tests/test_acceptance_probe_gate.py` 三件在本轮过 A2 面：ruff、`ruff format --check`、mypy、bandit 全部 0 问题（`a2-check` 会扫 `docs/evidence/**/*.py`，所以那台普查件必须在 A2 面达标；`tests/` 按 §A2 的分层定义只过 ruff 与格式）。`.env` 与任何密钥值全程未被读取或打印；本轮对真库零访问。

## 五、本轮没有做的事 / 须用户决定

1. **普查面 7 那 7 格 proven 而无判定面（`AC-16\|05`、`AC-16\|09`、`AC-17\|02/04/06/09`、`§4\|03`）一格没减**。它们各自需要的不是「再来一遍门禁」，而是有人为那一格写 measure。本轮只把它们钉进棘轮，让「以后新翻的 proven 不许再没有面」成为可检查的事。
2. **时刻面只在干净树上判**，而本轮所有遍次都跑在「还有东西在飞」的树上。这一点在本轮被量成了两截，不是笼统一句：档案 `git add` 之后，`AC-17|03`、`AC-17|08`、`AC-17|10` 三格已随 `gate-run2-prebackfill.txt` 读成 agrees（它们要的就是「文件进了索引」）；**仍没有回答的是 `AC-1|10`**——它读 `git status`，判据原文要「git 从未见过的文件 = 0」，本轮每一次遍次都注定不满足（一个轮次按定义如此）。⇒ 这一问要么提交后复跑（本轮的收尾提交之后由下一轮或 CI 回答），要么在 CI 上回答。**本轮从未 push**：CI 跑的是 `origin/dev` 上的旧提交，把这一切交给 CI 之前得先有人批准 push（task #52 的用户决策面）。
3. **普查面 6 那 15 个被 6 台仪器 import 的产品私有符号没有收口**，只普查了。收口的两个方向都不便宜：要么给这些符号开公开入口（动产品 API 面，会咬 `public-api-quality`），要么把仪器改成读公开面（动 C21/C26/C48/C49 的档案件，而档案件按规矩不许重写）。登记给 C51。
4. **红遍里那两条 bandit warning 没顺手清**：`[tester] WARNING nosec encountered (B603), but no failed test` 点名 `docs/evidence/C31/endpoint-contract-census.py:114` 与 `docs/evidence/C43/public-api-face-census.py:72` —— 别轮档案里多余的抑制注释。它们是 warning 不是 finding、不参与判定（本轮成功的那几遍里 bandit 输出根本不落进正文，所以也**不能据此说它们消失了**）；删注释等于重写别轮仪器，而本轮判据里没有这一项。登记为可选清理。
5. **`gap` 那 9 格本轮一格未动**：`AC-1\|03/05/07/08/09/10`、`AC-9\|08`、`AC-16\|06/07` 的判词里写着的都是用户决定面（措辞、联系方式、法务复核、起栈、经确认的落库写入）或另一轮的开发项，不是再改一次判定口径能过的。
6. 登记给 C51 的其余候选：`AC-9\|01/\|06/\|07`（task #62）；`PARTITION_MAINTENANCE` 的执行器（DDL 门）；akshare 退路成交额零值与混合单位的修数据半边；`batch_watermark` 从来没被写过。

## 六、本轮 diff 面

- `Makefile`：`acceptance-probe-check` target + `.PHONY` + `help` 的 A2 行 + `gate:` 两行（成员 16 → 17，绿时 18 段 `===== gate:`）。
- `scripts/quality/acceptance_item_probe.py`：`--gate-check` / `--sync-faces` 两个模式；`measured_readings` 与 `self_test_findings`（后者改为返回 `(findings, reading)`）拆开自 `self_test`；新增 `item_of` / `FaceBook` / `faces_of` / `face_book` / `read_face_baseline` / `face_diff` / `reconcile_state` / `reconcile` / `stale_cell_finding` / `verdict_lines` / `membership_findings` / `gate_check` / `sync_faces`；AC-17\|01 探针（14 条反事实）；模块 docstring 增补门禁模式一段。探针 30 → 31、反事实 229 → 243。
- `docs/quality/acceptance-probe-faces.json`：**新增**，`--sync-faces` 生成、必须进账；31 个探针里 12 个在读 moment 面，`proven_without_plane` 7 格。
- `tests/test_acceptance_probe_gate.py`：**新增** 33 条。
- `docs/evidence/C50/`：普查仪器 1 件（`probe_gate_census.py`；本轮内它自己改过一次——bandit B607 之后把 `["git", *args]` 换成 `shutil.which` 解析出的绝对路径，读数面逐字未变）＋ 读数档案 13 件（含 `gate-run3-postbackfill.txt`，它描述的是含本 README 这段文字之后的树）＋ 本 README。
- `docs/迭代计划/迭代1-重构数据中台/验收文档.md`：banner v5.21 → v5.22、§0 新增 v5.22 行、§10 AC-17 行回填（回合列表加 C50 并注明 C46–C49 未触碰 AC-17 判定面、证据面加本轮「0 / 46 → 每遍都跑」与两遍反证遍、push 范围改 `C5…C49 从未 push`）。**没有翻任何勾**，因此 `docs/quality/acceptance-item-ledger.json` 一字未改（台账字段记的是「哪一遍把这一格量成 proven」，本轮没有这样的格子）。
