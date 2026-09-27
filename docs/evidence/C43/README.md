# C43 — 公共 API 与证据可追溯两格，从「门禁打印一行」做成「条目级判定面」

对应判据：`AC-17|07`（**新增**公开 callable docstring 与参数注解覆盖率 100%）、
`AC-17|10`（历次里程碑的验证证据：命令 + 输出摘要 + 日期 可追溯）。
本轮之前这两格都是 `unreviewed`：AC-17|10 甚至没有任何机器面在数它，
AC-17|07 只有一行 `docstring coverage : 100.0% (601/601)`，而那行数的是一棵固定目录树。

本轮把 `evidence_traceability.py` 做成第 16 个门禁成员，并给两格各写了一个条目级探针
（`acceptance_item_probe.py` 里第 16、17 个探针，6 + 11 条反事实）。

## 一、量出来的五个缺陷（都不是「读数为 0 所以没事」那种）

| # | 缺陷 | 量法 | 修法 |
|---|---|---|---|
| 1 | A3/B3/C1 三个里程碑档案**有原始输出却没有一句叙述**：读者拿到的是一堆数字，不知道它证明什么、不证明什么 | `evidence_traceability.py` 的 `narrative` 面（每个轮次目录要有一份 `.md`） | 补写三份 README，只做加法，不改任何一份既有档案 |
| 2 | 「日期可追溯」此前**没有任何判据在数**：历次 gate 日志里，抬头约定（`采集时间` / `captured_at`）是 C 轮之后才形成的，早于约定的档案没人回头看过 | 新工具的 `date` 面：抬头 40 行内有 ISO 日期或 ctime 日期即算答出 | 见下面第二节的**收窄声明**：规则本身被这轮改了一次 |
| 3 | `_load_baseline()` 把「上限文件不存在」和「上限被冻结成空列表」读成同一件事 | `teeth-planted.txt` TEETH-1：基线已冻结为 0 条时，种进一份违规档案打印的是 `FAIL: no baseline yet; run with --update` —— 建议当事人**把刚种下的违规冻结进上限** | `_load_baseline()` 改为 `list[str] \| None`：缺文件才是 `None`，走「从未冻结」分支；存在但为空就是「本轮一切违规都是新增违规」 |
| 4 | `public-api-quality` 数的是**固定七棵目录**（`A2_SCOPE`），本轮新写的 17 份 `scripts/ops/*.py` 与 `opendata/api/*` 等 46 份自研文件全在视野外；而直接把全树拉进来会得到 1930 个「违例」，因为它们绝大多数是 `tests/`（ruff 按配置在那儿关掉 D/ANN） | `public-api-face-census.txt`：按人群拆开数（见第三节表） | 不改门禁项的口径（那是 `docs/CODE_QUALITY.md` 的 A1/A2 分层），改为在**条目级探针**里加一条加宽面：A2 文件集 ∩ 自研 ∩ 不在固定目录 ∩ 不是 tests/docs，用同一个 `collect_symbols` 再量一遍 |

| 5 | 新成员把自己那一跑的运行日志判红（半截文件没有 exit 读数、也没进索引） | `gate-run1.txt`：`GATE_EXIT=2`，两个 `NEW VIOLATION` 都点名 `docs/evidence/C43/gate-run1.txt` | 改归档流程而不是改判据：日志先写到 `docs/evidence/` 之外、运行返回后才落档（第八节）。前后 15 个成员都不会读 `docs/evidence/`，所以这个失败模式是本轮**新引入的** |

缺陷 1 与 2 是档案面的真实缺口，已修；缺陷 3 是本轮工具自己的判定面缺陷（恒真的一面读数的
反面：一个把「已清零」读成「还没开始」的分支），已修并有反事实；缺陷 4 是覆盖面缺口，
用加宽面补上而**不是**把门禁标准放水或扩大到 A1 存量；缺陷 5 是流程缺陷，判据本身没被放宽。

## 二、收窄声明：`date` 面被这轮改窄了一次，理由是量出来的

新工具第一次普查报出 6 份 gate 日志缺日期。逐份读原文（`date-face-census.txt`）：

```
docs/evidence/A2/gate.txt       2026-09-23 00:30:27.034 | INFO | opendata.core.token_blacklist…
docs/evidence/A4/gate.txt       2026-09-23 07:20:43.343 | …
docs/evidence/A5/gate.txt       2026-09-23 10:12:56.502 | …
docs/evidence/C4/gate.txt       2026-09-24 14:14:27.719 | …
docs/evidence/C9/gate.txt       2026-09-24 23:54:38.994 | …
docs/evidence/C13/gate.txt      2026-09-25 02:51:42.561 | …
```

这六份都**答得出「哪天」**——时间戳是运行自己写进日志的（loguru 行、pytest/npm 时钟），
只是不在抬头 40 行里。于是原来的规则量的其实是「日期写在第几行」，不是「档案答不答日期」。
处理方式是把规则改成两个读数之和（抬头写明的 + 运行自己打下的），并在 `run()` 里把两个
计数都打印出来（`dates in gate logs = header 63/69, printed by the run 6/69`），
使这次收窄是**看得见的**，而不是把一个缺口悄悄删掉。

**没有改动这六份档案的任何一行**。反事实也做了：AC-17|10 的探针要求
`header + run == gate 日志总数`，把 `dated_run` 归零就翻红（11 条反事实之一）。

## 三、公共 API 面的人群普查（`public-api-face-census.txt`）

对 1359 个 tracked-or-A2 的 `.py` 路径，用 `public_api.collect_symbols` 数：

| 人群 | 文件 | 公开 callable | 不达标 |
|---|---|---|---|
| 1 门禁固定目录内（现在数的是这个） | 150 | 611 | 0 |
| 2 `tests/`（ruff 关掉 D/ANN、a2_check 关掉 mypy/bandit） | 162 | 3370 | 3181 |
| 3 `docs/`（随证据归档的复算脚本，不是对外 API） | 48 | 206 | 0 |
| 4 `a2_check` 自己排除的根树（搬运层 / 前端 / 迁移） | 323 | 1288 | 303 |
| **5 自研、新增或触碰、但在固定目录之外 —— 本轮加的加宽面** | **46** | **222** | **0** |
| 6 自研 A1 存量（A0 基线后未触碰，按「触碰即达标」） | 47 | 118 | 6 |

人群 5 = 46 份文件 / 222 个公开 callable / **0 个不达标**，这就是 AC-17|07 现在真正挣到的部分。
人群 6 那 6 个不达标是 `opendata/api/metrics.py:166,171` 与
`scripts/init_tasks_and_tables.py:33,106,177,221`：两者都由 A0 基础提交 `4ee1bbc` 引入，
按 `CODE_QUALITY.md` 的分层属于 A1 债务，本轮**如实打印但不用它判定**（写进读数第三行，
数字往下走就是往下走）。人群 2/3 的 1930 也照原样打印在第四行。

## 四、反空壳读数（`teeth-planted.txt`）

「种下 → 运行 → 立刻拔除 → 复跑」，四段都有：

| 段 | 种下什么 | 期望 | 实读 |
|---|---|---|---|
| TEETH-1 | 一份四问皆无的假 gate 日志 + 一个没有 README 的轮次目录 | 六个面同时变红 | `narrative/date/identity/command/exit/untracked` 各 +1，`FAIL: 6 new traceability violation(s) against a 0-entry baseline.`，exit 1 |
| TEETH-2 | 固定目录内一个没有 docstring 的公开函数 | 门禁项自己变红 | `99.8% (611/612) … missing docstring`，exit 1 |
| TEETH-3 | 同一个缺陷种在固定目录**之外**（`scripts/teeth_c43.py`） | 门禁项看不见、条目级加宽面看得见 | `public_api.py` 依旧 `100.0% (611/611) EXIT=0`；探针读数 `47 file(s) / 223 callable(s), below standard = 1 (scripts/teeth_c43.py:1)` → `VERDICT AC-17|07: gap` |
| TEETH-4 | 把冻结上限文件移走 | 必须与「冻结成 0」区分开 | `FAIL: no baseline yet; run with --update…`（这是唯一该出现这句话的情形） |

拔干净后的复跑：`find` 命中 0 个残留，`public_api.py` 回到 `EXIT=0`。
（`teeth-planted.txt` 末段 CLEAN 那次 `evidence_traceability.py` 仍是 exit 1，报的是 C43 自己
当时的 5 个未跟踪文件与 1 个尚未写的 README —— 那是采集时的在途状态，
`teeth-planted.txt` 末段 CLEAN-2 是这些文件都进索引之后的那次读数，六个面全 0、exit 0。）

## 五、门禁成员数变了

`make gate` 从 15 成员 + PASSED（16 个 `===== gate:` 段）变成 **16 成员 + PASSED = 17 段**：
新增成员 `evidence-traceability` 排在 `ledger-check` 之后。

这句更正登记在 §10 AC-17 的**说明列**（沿用 C39 那格「C39 起门禁为 15 项、完整日志 16 个标记」的写法），
**没有**改 §2 条目 `AC-17|01` 的原文，也没有回头改历次档案里的成员数。
理由是这台机器的一条规则：`acceptance_ledger_check.item_key` 对条目**整行原文**取摘要，
一改原文，台账里的 `AC-17|01|eeaa81ec` 就变成另一格的身份 —— 而「措辞改动会把条目孤立成发现」
正是它要提供的信号，不该为了加一句注释把它用光。成员清单那句原文里本来就写着「另有 `secret-check`／
`ledger-check`／`js-points-check`」这类**举例**，17 段的完整清单以 `Makefile` 与门禁日志为准。

## 六、这两格现在证明什么、不证明什么

证明：

* 425 份证据档案（53 个轮次目录，其中 gate 日志 71 份）逐面可读：每份轮次有叙述、
  每份 gate 日志答得出日期（抬头或运行时戳）、身份（commit）、命令、exit；冻结上限 0 条 ⇒
  今后任何一条新违规都会让 `make gate` 变红。
  这组计数取自本轮最后一次读数（`gate-run1b-prebackfill.txt` 落进索引之后），
  与前面几份档案里的 423/424 不同是**因为档案在长**，见第八节末段。
* 证据档案从未被删过（`git log --diff-filter=A` 与 `git ls-files` 的差集为 0）。
* 门禁固定目录之外的 222 个新增/触碰公开 callable 与目录内的 611 个一样是 100%/100%。
* 两个判定面各有 6 条与 11 条反事实，全部能把一份「干净读数」翻回 gap；
  `--self-test` 17 探针 83 反事实绿。

不证明：

1. **不证明「历次里程碑都真的跑过」**。工具查的是档案里*写没写*命令与 exit 读数，
   一份伪造但格式齐全的日志能过 `command`/`exit` 面 —— TEETH-1 反过来用正是这个意思：
   它能抓住「什么都没写」，抓不住「写了但没跑」。可交叉的只有 `identity`（commit 是否在那个
   轮次之前）与 git 首见日期，两者都只是必要条件。
2. **不证明 A1 存量的公共 API 达标**：人群 6 那 6 个不达标仍在，按分层由棘轮管，不由本判据管。
3. **不证明 tests/ 的断言质量**：人群 2 的 3181 个「不达标」里有很大一块是 ruff 配置允许的
   测试辅助函数，本轮把它们排除在判定面之外（并且明写排除理由与规模）。
4. **不证明两个探针已进门禁**：`acceptance_item_probe.py` 仍不是门禁成员，它产出台账输入；
   本轮翻进台账的只有 AC-17|07、AC-17|10 两格。
5. `evidence-traceability` 的六面里 `untracked` 面依赖索引：本轮档案是先 `git add` 后才取得
   干净读数的，因此那份读数证明的是「将被提交的这棵树」干净，不是提交之后仍然干净。

## 七、复算

```bash
export PATH="$HOME/opt/anaconda3/envs/py313/bin:$PATH"; export PYTHONPATH=.
python scripts/quality/evidence_traceability.py            # 六个面 + 两个日期计数
python scripts/quality/public_api.py                       # 门禁固定目录那 611 个
python scripts/quality/acceptance_item_probe.py \
  --item 'AC-17|07' --item 'AC-17|10'                      # 两个条目级判定面
python scripts/quality/acceptance_item_probe.py --self-test  # 17 探针 / 83 反事实
python docs/evidence/C43/public-api-face-census.py         # 人群拆分（六行表）
```

`teeth-planted.txt` 里每段的种/拔命令原样可重放；本轮没跑 `--update` 之外的写操作，
没有对任何数据库做 DDL，也没有改动 A0～C42 的任何一份既有档案（A3/B3/C1 三份 README 是新增）。

## 九、三遍门禁各自证的是哪棵树

| 遍 | 档案 | 树 | 读数 |
|---|---|---|---|
| run 1 | `gate-run1.txt`（99 行／5 段／`GATE_EXIT=2`） | 回填前，但新成员读到自己在写的日志 | 红在 `evidence-traceability`，见第八节 |
| run 1b | `gate-run1b-prebackfill.txt`（7,037 行／17 段／`GATE_EXIT=0` 在第 7032 行） | **回填前**的最终代码树 | `3076 passed／6 skipped in 63.13s`、`TOTAL 11097 1090 2738 286 88.71%`、`A2 files: 336`、public-api `611/611` 双 100%、`ledger-check items=130 proven=18 gap=7 unreviewed=105 ticked=18`、traceability 六面 0（`files census = 424 (gate logs: 70)`，`header 64/70 + printed by the run 6/70`） |
| run 2 | `gate-run2-postbackfill.txt`（7,040 行／17 段／`GATE_EXIT=0` 在第 7035 行） | **回填后**（v5.15 文档 + 台账两格 proven + 本轮档案已 `git add`） | 与 run 1b **逐字相同**的判定面读数，只差两处账面输入：`proven=20 gap=7 unreviewed=103 ticked=20`、`files census = 425 (gate logs: 71)` 与 `header 65/71 + printed 6/71` |
| run 3 | `gate-run3-final.txt`（7,041 行／17 段／`GATE_EXIT=0` 在第 7036 行） | **最终树**：run 2 那份档案与 README 第九节已落档并 `git add` | 判定面读数与前两遍**逐字相同**（`3076 passed／6 skipped in 62.00s`、`TOTAL 11097 1090 2738 286 88.71%`、`A2 files: 336`、public-api `611/611`、ratchet `debt did not increase`、secret `no leaks found`），只有普查分母跟着档案长：`files census = 426 (gate logs: 72)`、`header 66/72 + printed 6/72`（两者之和正好覆盖 72 份） |

三遍的覆盖率行与用例数完全相同 ⇒ 「run 1b 之后 `opendata/` 与 `tests/` 一行未改」是可核对的事实而不是叙述；
差的正是 `ledger-check` 与 `evidence-traceability` 这两个**以本轮改动为输入**的成员，前一遍的绿不替后一遍作证。
run 2 之后落进 `docs/evidence/` 的是它自己那份档案（`.txt`，`a2_check` 只收 `.py` ⇒ 不入清扫面）与 README 的本节，
所以 run 3 证的是「分母含 run 2 那份」的树。**这条递归不会闭合**：任何一遍都不含它自己的日志，
因此「最后一遍」的说法只在它自己的分母意义上成立；run 3 之后剩的改动全是这三份 markdown 的字句，
其影响面由 `post-backfill-members.txt` 逐个成员复算（`ledger-check`／`brand-check`／`evidence-traceability`／`secret-check`），
而不是把 run 3 那句话扩到它没证的东西上。

## 八、新成员第一次跑就把自己的运行日志判红了

`gate-run1.txt` 是本成员进 `make gate` 之后的第一跑，`GATE_EXIT=2`，红在它自己那一段：

```
  files census  = 424 (gate logs: 70)
  exit       gaps = 1
  untracked  gaps = 1
    exit: docs/evidence/C43/gate-run1.txt
    untracked: docs/evidence/C43/gate-run1.txt
FAIL: 2 new traceability violation(s) against a 0-entry baseline.
```

被点名的这份档案就是**这一次运行正在往里写的那一份**：它被重定向到 `docs/evidence/C43/` 里，
成员扫到它时它还没有尾行的 `GATE_EXIT=`（⇒ `exit` 面读不到），也还没 `git add`（⇒ `untracked` 面命中）。

三条结论分开说：

1. **不是产品代码缺陷**：`opendata/`、`tests/` 一行都不用改，成员的判决在它的规则下是对的
   ——一份没有 exit 读数、没进版本库的 gate 日志确实不满足 AC-17|10。
2. **是流程缺陷，而且这个缺陷只对新成员可见**：本仓的归档约定是「完整未裁剪日志 + 运行前采集的
   抬头」，此前 15 个成员没有一个会去读 `docs/evidence/`，所以「日志写到一半就被判定面扫过」这件事
   从未成为一个可能的失败模式。C42 那轮的 `gate-run2-final.txt` 若跑在新成员在位时，会红得一模一样。
3. **处理方式是改流程而不是改判据**：从这一跑之后，`make gate` 的输出先写到 `docs/evidence/`
   **之外**的暂存路径，运行返回、`GATE_EXIT=` 追加完毕，再连同抬头落成 `docs/evidence/<round>/`。
   `run 1b`（`gate-run1b-prebackfill.txt`）就是按这个流程跑的，`GATE_EXIT=0`，
   且它自己的档案在它运行期间根本不存在——所以那一遍的六个面全 0 **不是**因为它躲开了检查，
   而是因为它检查的是上一遍已经落定的档案集合。

顺带一条同类读数的口径：本成员的 `files census` 会随本轮档案的落地而增长
（`probe-ac17.txt` 前段读到 52 轮／423 份／69 份 gate 日志，`gate-run1b` 读到 53／424／70），
所以这些计数描述的是**那一刻的树**，跨档案对照时以各份自带的 census 行为准，
不要拿两遍的计数当成同一个量。
