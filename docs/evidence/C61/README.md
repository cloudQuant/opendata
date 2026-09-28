# C61 —— AC-5 的七格从 0/7 变成 4/7，其中一格是靠「标注不可用并登记」那条支路挣到的，另外三格缺的都是仓库外的一句话

## 1. 本轮做什么

AC-5 是搬运层（`opendata_http/`）的七条判据，此前状态是 `unreviewed`、条目级 0/7 —— 也就是说 §10 里
那句「manifest 校验通过、字节级保真」从来没有一格被逐条量过。本轮按 C43/C45/C55/C58 的形状给每条
做 `measure/judge/breaks/repair`，全部只读、离线、不碰生产仓库：**proven 4 格（|01/|04/|05/|06），gap 3 格
（|02/|03/|07）**，判据原文一字未动（§2 只把 `- [ ]` 翻成 `- [x]`）。

仪器面：`scripts/quality/acceptance_item_probe.py` +971 行（全部是新增，7 个探针与它们的反事实），
`scripts/codemod/report_port.py` +93 行（资源不可用登记段落），`docs/port-report.md` +13 行（生成结果）。
全表反事实自检从 47 探针/378 条 break 涨到 **54/429**，每条 break 都能把干净读数打回 gap
（档案 `probe-self-test.txt`）。守卫 42 条 passed（`guard-tests.txt`，两个 MySQL 变量钉到无监听的
127.0.0.1）。**这两处随后又被 §7 那一格推了一次**：break 加到 430、守卫按另一对文件跑到 44，
终值以门禁遍（`gate-run1.txt` 的 `acceptance-probe-check` 成员）为准。

## 2. 四格 proven 各自凭的是什么

| 格 | 判据的哪一句 | 现场读数（逐字在 `probe-items-ac5.txt`） |
| --- | --- | --- |
| `AC-5\|01` | 「逐项校验通过（文件清单 + sha256 + 资源清单 + 计数）」 | manifest 315 条逐条现算：sha 不符 0、磁盘缺文件 0、磁盘有而清单无 0、清单有而磁盘无 0；资源清单恰好等于树里那 2 个非 py 文件；四个计数（313 py / 2 资源 / 315 合计 / 93,205 行）逐个与现算相等；`upstream.lock` 文件集与 manifest 相等；`gen_manifest.py --check` 打 `OK: manifest.json is up to date`。**写死的模块数字不再参与判定** |
| `AC-5\|04` | 「根目录 `akshare/` 已删除；未安装 akshare 的环境中 P0 域函数可用」 | 磁盘无 `akshare/`、git 索引该前缀 0 个文件、`find_spec('akshare')` = absent、`import opendata_http` exit 0；首方代码取用的 11 个端点函数逐个 `callable(getattr(...))`，取不到 0；干净 venv 侧证读 C55 那轮自写的档案（`akshare: absent`），本轮没有伪造这份证据 |
| `AC-5\|05` | 「每文件头保留 MIT 版权声明 + 来源标注」 | 清单里 313 个 py 文件全树走查（磁盘与清单文件集相等，所以走查面就是清单面）：缺 MIT 声明 0、缺来源行 0、缺 upstream URL 0；文件头点名的 commit 只有 **1** 个且逐字等于 `upstream.lock` 里那一个；`THIRD_PARTY_NOTICES.md` 在位 |
| `AC-5\|06` | 「`datasets.py` 的资源访问函数可运行（**或明确标注不可用并登记**）」 | 两条支路里走的是第二条：2 个访问函数当场调用，返回路径 0 个、raise 里明说 unavailable 2 个、报别的异常 0 个；两条 raise 文本都回指 `docs/port-report.md`；报告里那一节有且只有 2 行、正好点名这两个函数；清单登记的 2 个内置资源在磁盘齐备 |

`|06` 值得多说一句，因为它最容易变成自证：判据允许「标注不可用并登记」，但**登记不是往文档里贴一段话**。
这段落必须由 `report_port.py` 从 `datasets.py` 的 raise 语句与 `upstream.lock` 派生 —— 因为
`docs/port-report.md` 整体是生成物，还要与已提交档案逐字节比对（`AC-17|05` 的重放面）。手写的段落会让下一次
重放把差异判成回归。所以本轮改的是生成器，登记处随之可重放；探针再判三件事：raise 文本确实回指该文件、
章节标题在位、表体行数与「实测 marked 的函数」一一对应且不多不少（多一行就是有人没被 raise 也被登记，
少一行就是登记漏了）。把这两个函数真改成读搬运树内的路径要改 `datasets.py` 正文，那会破坏与 upstream.lock
的确定性重放，属搬运基线决策，本轮不替它做。

## 3. 三格 gap 缺的不是读数，是仓库外的一句话

- **`AC-5|02`（P0/P1 域子模块搬运完成）**：缺一个**机器可读的分母**。`domains.yaml` 与 `Capability` 都不带
  batch/priority/tier 字段，需求 D9 与实施计划 B1.1 的子模块清单写在「stock、stock_feature、…、utils **等**」
  这类散文里。「完成」没有分母就只能靠叙述。已量的两半是实的：首方代码取用的端点 11/11 全导出，被点名移出
  本迭代的 8 个非金融子包 0 个混进来；D9 点名范围内真正没搬的只有 `futures_derivative` 1 个（见 §4 那条缺陷）。
  钉出这个字段是产品/接口决策 —— 谁声明哪个域属哪一批，本轮不替它编。
- **`AC-5|03`（AST 级零 `akshare` 运行时引用，含字符串常量与动态导入）**：import 形态 0、动态 import 0，
  基线剩下的 3 条全是产品事实的名字面量（`openbb_map.py`、`data_script.py`、`patrol.py`）。抹平它只有两条路，
  两条都要改判据本身：①把扫描器字符串规则改窄 = 回头放宽已勾的 `AC-16|05`「AST 口径」；②改掉这三处真实数据源名
  的拼写 = 给扫描器做伪装，正是 C35 收口过的门禁空转。**同一道题 `AC-16|06` 已经登记为用户/产品决策（task #64）**，
  这里不重复改判、也不换个说法再判一次。
- **`AC-5|07`（搬运层安全扫描完成并人工 triage 留档）**：本轮把「留档 vs 今天的树」这件事量出来了。A2 那份 triage
  自己声明扫的是 **131** 个 py 文件（**328** 项），搬运树今天是 **313** 个（本轮全量 **1021** 项、**234** 个文件带
  finding、**13** 条规则：B105/B107/B110/B112/B113/B301/B307/B311/B324/B403/B404/B501/B603），而 triage 只处置了
  **10** 条规则 —— 于是 **5** 项落在从未处置过的规则上：`B403@futures/cons.py:13`、`B301@futures/cons.py:576`、
  `B324@stock_feature/stock_a_indicator.py:50`、`B324@stock_feature/stock_info.py:36` 与 `:37`。更难看的是那句
  免罪句「无 B608、**无 B324**、无 pickle/yaml.load 类反序列化项」——它点名的 B324 今天真的出现了 3 项；
  反序列化那一半本轮按 bandit 自己的测试名匹配到 0 条，所以那句还成立，但成立的原因是匹配口径而非树里真的没有，
  读数里两条分开写、不合并成一句「triage 依旧正确」。
  逐条人工处置是这件事的正当收口，本轮不给它补假处置。

## 4. 本轮最该记住的两件事：仪器的两处自欺，都是复算后才露出来的

1. **`|02` 的「缺失子包」有一条是解析残渣。** 需求 D9 的原文是「…、economic、utils 等」，按「、」切出来的最后
   一段是 `utils 等`，于是磁盘上明明存在的 `utils` 被判成没搬。第一版读数因此写着「缺失 2 个：futures_derivative、
   utils 等」—— 其中一个是假的。修法是剥掉行尾的「等」再比（不是把 `utils` 特判掉）。判据结论没变（这格仍是 gap，
   承重的是缺批次字段），但**gap 的理由必须是真的**。
2. **`|07` 的 `target_ok` 可以被一行注释养活。** Makefile 的 `security-ported` recipe 头两行是 `@# …` 注释，
   旧读数把它们一起拼进命令再截断，打印出来只有注释、看不见真正执行的 `-bandit -r opendata_http …`；更糟的是
   `PORTED_ROOT in recipe` 这个判据会被注释里的路径名满足 —— 哪天有人把命令改扫别处而留着那句注释，这格照样「yes」。
   现在注释行（`#` 与 `@#`）不进 recipe，读数直接印真正的命令。
   
   两处都属于「判据没坏、读数在撒谎」，都不是靠放宽判据修的。

## 5. 本轮没做的事，以及为什么

- **没跑 `make security-ported`。** 它的 `-o` 参数硬写 `docs/evidence/A2/bandit-ported.json` —— 那是 A2 那一轮的
  档案，覆写它就是改写历史证据。本轮用同一条 bandit 命令自己扫、写到 `docs/evidence/C61/ported-bandit-scan.json`，
  并在档案头里把命令、版本、时刻与「为什么不覆写 A2」写清楚。这条命令带 `-` 前缀（make 忽略退出码），因为
  搬运层有 finding 是预期输出，判据要看的是产物不是退出码。
- **没动搬运树正文、没动 `opendata/data/domains.yaml`**（批次字段属产品决策，见 §3）。
- **没跑任何 e2e 腿、没对生产仓库连接或写入**；守卫那一遍把两个 MySQL 变量钉到无监听地址。
- **`|06` 没顺手改成「可运行」**：改它要动 `datasets.py` 正文，会破坏 `upstream.lock` 的确定性重放面。

## 6. 台账与文档面

§2 翻 4 格（`AC-5|01/|04/|05/|06`），台账 7 格全部从 `unreviewed` 落成实状态（4 proven + 3 gap，各带读数与理由）；
§10 的 AC-5 行 `条目级 0/7` → `条目级 4/7`，其余叙述一字不改。census 由 `130 proven=43` 变 `proven=47`，
gap `11 → 14`、`unreviewed 76 → 69`（7 格里 4 进 proven、3 进 gap，一格都不留白）。

`AC-5|04` 有一张时刻面（`index(git ls-files)`，见 `docs/quality/acceptance-probe-faces.json` 本轮新增的 7 行）：
脏树下它走 deferred、不判 proven，所以这格的 proven 最终要由清洁树上的门禁遍（`--gate-check`）复核；
其余六格 `moment_faces: []`。

## 7. 翻完台账之后才看见的第三处：本轮给 `AC-5|06` 造的登记段，把已 proven 的 `AC-17|05` 判倒了

门禁遍跑到一半被我主动停掉（不是失败，是它必然失败：与其花 33 分钟拿一份红的门禁当本轮的证，
不如先量清冲突）。事情本身是这样：

- `report_port.py` 生成的「内置资源不可用登记」表，行首写成 ``| `get_ths_js` | ``；而 `AC-17|05` 数
  「每个搬运文件一行」用的判据就是 `line.startswith("| `")`（`port_replay()`）。两条登记行于是被算进
  重放行数：**`rows_total` 317 ≠ `upstream.lock` 的 315**，`judge_ac17_05` 的等式两边对不上 ⇒
  那格从 proven 变 gap。**本轮没碰 `AC-17|05` 的任何判据，却把它判倒了** —— 这是生成物形状与
  另一个判据的口径撞车，不是新判据太严。
- 数字不是回忆：档案 `ac5-06-shape-fix.txt` 头部那段是用 `git show 759be9c:docs/port-report.md`
  的原始字节现算的两个计数（317 / 315），锁记录数走 `lock_records()`，与探针同一个前缀常量。
- **修法改的是生产物，不是判据**：登记行不再加反引号（`report_port.py` 的行模板），`AC-5|06` 的行解析
  改成按首列形状认数据行（首列是标识符才算，表头是中文、分隔行是连字符，自然被排除）。
  没有去改 `port_replay()` 的前缀或 `judge_ac17_05` 的等式 —— 那等于把一条已勾判据的口径改窄，
  和 C60 §6 禁止的「改 `python_version` 洗绿」是同一类捷径。
- 这件事现在是**被量住的**，不是靠约定：`AC-5|06` 新增面 `shape_clash` = 登记段里与重放表同形的行数，
  今天读数 0；它进 judge 的顶层合取，并配一条反事实（施加 `shape_clash=2` ⇒ 该格回 gap）。
  谁再把反引号加回去，两格同时变红，而不是只有一格在骗人。
- 正控不是空跑：把 759be9c 的**真实**登记段喂进今天的 `measure_ac5_06`，读到 `shape_clash = 2`、
  `register_rows = 0`（解析器按形状认行，旧形状一行都不算数据行）⇒ 判定回 gap。
  这条同时证明新解析器不是「换成另一种硬编码形状」，而是真的对形状不敏感。
- 修复后读数（`ac5-06-shape-fix.txt` S2/S3）：`AC-5|06` proven（`shape_clash` 0）、
  `AC-17|05` **回到 proven**（锁 315 / 报告 315 行 / 逐字一致 315 / 现场渲染与档案逐字节一致 = yes）。
  `--sync-faces` 写回后面基线与已提交版本**逐字节相同**（新面不读 git 时刻），所以本轮没有需要人签的漂移。
- 静态面与守卫：两台仪器 ruff / format / mypy / bandit 全清，`test_acceptance_probe_gate.py`(38) +
  `test_ported_import_closure.py`(6) = **44 passed**。§1 那句「守卫 42 条」是同一轮前一遍的另一对文件
  （38 + `test_p0_integration_surface.py` 的 4），数字不同是因为**跑的文件对不同**，不是回归。
- 两条如实边界：①正控脚本是一次性仪器，没随档留档（复现口径 = 把 `git show 759be9c:docs/port-report.md`
  的字节喂给 `measure_ac5_06`，见档案 S4 段首那行）；②这层形状耦合由**探针面**守着，不由单测守着 ——
  `grep report_port tests/` 命中 0 个文件，`collect_resource_register` 的输出形状从来没有任何用例钉过。

## 8. 档案表

| 文件 | 内容 | 证据档位 |
| --- | --- | --- |
| `probe-items-ac5.txt` | 七格读数全文（77 行正文，4 proven/3 gap + 末行 census），出处头写明命令、判据契约行号与两处仪器缺陷的复跑说明；头里 12 处行号引用逐条机器复核 | 仪器自报（管道尾是 tee，上游 `$?` 不可得 ⇒ 退出面以打印契约为准，头里如实写） |
| `probe-self-test.txt` | 全表反事实自检：54/54 探针走到判定、429 条 break 全部把干净读数打回 gap | 仪器自报（完整 stdout 就是那 2 行，`tail -40` 未裁） |
| `guard-tests.txt` | `tests/test_acceptance_probe_gate.py`（38）+ `tests/test_p0_integration_surface.py`（4）= 42 passed，`-m "not e2e" --no-cov`，MySQL 变量钉死 | 节点面实测 |
| `register-and-diff.txt` | 三处改动 diff --stat（+1077 全为新增）、`gen_manifest.py --check` 的 exit 0、报告里新生成的「内置资源不可用登记」整节 | 命令输出原样 |
| `ported-bandit-scan.json` | 本轮搬运层全量 bandit 产物（1021 项 / 313 文件树 / 13 条规则）+ `archive_round=C61` 自写标识与「为什么不覆写 A2」的说明 | bandit 原始 JSON 全量，未裁 |
| `ac5-06-shape-fix.txt` | §7 那一格：头部现算的 317↔315 冲突计数（输入是 `git show 759be9c:docs/port-report.md` 的字节）、两台仪器的静态面与 44 条守卫、修复后 `AC-5\|06`（含 `shape_clash=0`）与 `AC-17\|05` 的条目读数、形状面正控 + 6 条反事实施加、`--sync-faces` 无 diff | 仪器原样输出，5 个段索引行号写定后逐条回读命中（末行 `CITATIONS=OK`） |
| `README.md` | 本文件：叙述面（§1–§6 是翻账那一段，§7 是停掉门禁遍之后补的那一段） | 叙事 |
