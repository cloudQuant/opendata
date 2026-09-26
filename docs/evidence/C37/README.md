# C37 —— AC-6 保真对照的 float-repr 面：判据对齐文档原话，并把「为什么 CI 会红」量到底

> 轮次：C37 · 日期：2026-09-26 · 分支 `dev` · 判据文件 `scripts/codemod/compare_with_upstream.py`（sha256
> `8ff0c8490cd13b6654951ad9e898fb10ccfedda0bef73447a118d9eb5e3a38ec`）
> 一句话：`test_ported_output_matches_upstream[financial_statement]` 在 CI 上连红三次（2026-09-23 的
> run 35805289557 / 35823639209 / 35840060209），红的是**同一个钱数的最后一位有效数字**；本轮把判据从
> 「repr 逐字符相等」改成 AC-6 自己写的「取值一致（浮点在容忍度内）」，并用三次变异证明这不是放水。

---

## 1. 判据面：改了什么，为什么这一改不放宽任何东西

AC-6 条目原文是「P0 域函数与 akshare 原实现做录制回放对照：字段结构与**取值一致（浮点在容忍度内）**」。
改前的实现里，这句话只有一半生效：dtype 记成 `float*` 的列走 `np.isclose(rtol=RTOL)`，而 dtype 记成
`object` 的列走 `str(value)` 逐字符比 —— 财务报表这一族恰好整列都是 `object`（一列里混着 float 和
`--`/字符串），于是**数值上完全等价的两个格子会因为文本最后一位不同而判红**。

本轮的改动只有三处，都在 `scripts/codemod/compare_with_upstream.py`：

1. 文本分支改为逐格 `_text_cell_diffs()`：先比文本，不等时再看**是否真有一侧是 float 实例**
   （`_float_pair()`，`float` / `np.float64` / `np.floating`）。是，才用 `np.isclose(..., rtol=RTOL)`
   复判；不是（两侧都是字符串），维持逐字符相等 ——  `'000001'` 与 `'1'` 这种代码列绝不会被读成同一个值。
2. 「被容忍」不等于「被忽略」：凡文本不同而浮点一致的格子必须写进 `notes`，内容是
   `cell 列[i]: 文本不同但浮点在 rtol=1e-09 内一致（左 -> 右，abs=…，rel=…）`；
   runner 把这些 note 收进 `results[i]["tolerated"]`，报告表头新增一列「文本不同而浮点一致」。
3. `RTOL` 一个字没动，仍是 `compare_with_upstream.py:54` 的 `1e-9`。

新增六条用例（`tests/test_port_fidelity.py:215-276`）把这条分支的两端都钉住：既钉「差 1 ULP 不判红」，
也钉「真换了数必须判红」「恒等时不许留 note」「纯文本格仍要逐字符」「代码列不算数」。

**这条新分支在本机的 16 例对照里没有一次被走到**（见 §2 第 2 条）：`docs/evidence/A2/compare-report.md`
本轮的 diff 只是表头多了「文本不同而浮点一致」一列、16 行读数全为 `0`，逐例 `result` 与改动前相同。
所以本轮的判据面证据是**用例与变异**，不是「16 例里终于绿了」——写反了就是自指测试。

### 变异巡回：证明它会咬人（`mutation-proof.sh` / `mutation-proof.log`）

| 变异 | 改坏的东西 | 期望 | 实测 |
|------|-----------|------|------|
| base | 不改 | 整套件绿 | 25 passed / 6 skipped，exit=0 |
| base-guards | 不改 | 六条守卫绿 | 6 passed，exit=0 |
| M1 | 文本分支退回按 repr 判 | 红 | 2 failed（正是 CI 那一格 `'…41' != '…40997'`），exit=1 |
| M2 | `RTOL` 放宽到 `1e-2` | 红 | 1 failed（`test_value_change_in_object_column_still_fails`），exit=1 |
| M3 | 取消「至少一侧是 float 实例」的门槛 | 红 | 1 failed（`…_looks_numeric_is_not_numberified`），exit=1 |
| restored | 还原 | 绿 | 25 passed / 6 skipped，exit=0；源文件 sha256 逐字节还原 `8ff0c849…` |

M2 就是「放水会被立刻抓到」的那一条：**如果本轮是靠放宽容忍度让 CI 变绿，档案里第一行就会红。**

---

## 2. 归因链：六格红是哪来的（全部是量出来的，不是推测）

`attribution.py` 一份脚本按 `[0]…[6]` 七面量下来，`attribution.log` 里同一份脚本在**两套栈**各跑一遍
（RUN A 本机 pandas 2.3.1 / numpy 2.3.1，RUN B 装成 CI 解析出来的 pandas 3.0.6 / numpy 2.4.6），
两份退出码都是 0。

1. **夹具不是变量**：`reference.csv.gz` 里这六格写着 `218472857114.40997` 一类文本，与 CI 报错的**左值**
   逐字符相同（`[1]`）。CI 读到的就是仓库里这一份 —— 连 CI 当时那个 commit（`e2f1c60`）里的夹具字节也一起
   比过（`[4d]`）。
2. **本机回放不红**：把录制响应喂给搬运层，本机六格算出的值与夹具文本逐字符相等，
   `diff=0 条 / 容忍度说明=0 条`（`[2]`）—— 也就是说这一格在本机连新分支都不进。
3. **上游给的是 18 位有效数字**：新浪响应体里是 `"item_value":"218472857114.409970"`，不是两位小数（`[3]`）。
   所以分歧只可能发生在「字符串 → double / → 文本」这一步。
4. **本机 10 条转换路径全列位型**（`[4b]`）：`float()`、`pd.to_numeric`、`Series.astype(float)`、
   `pd.read_csv` 的 default / `high` / `legacy` / `round_trip`、整数拼接再除 10^k、按 15/17 位有效渲染后读回。
   唯一命中 CI 右值位型的是 **`%.15g` 渲染**（六格 6/6）。
   `read_csv legacy` 给的是 `…40994` —— 方向相反（比正确值小 1 ULP），当场否掉了「pandas 用了低精度核」这条。
5. **`.15g` 命中意味着什么，必须说清楚**：CI 那个文本既可以是「比正确值大 1 ULP 的 double 的最短表示」，
   也可以是「同一个正确 double 被按 15 位有效数字印出来」。**两种解释产生的文本一模一样，从 CI 日志分不开。**
   本轮不需要分辨它：两种都是「同一个数、最后一位有效数字的呈现差」，rel=`1.4e-16`，
   落在 AC-6 自己的 `rtol=1e-9` 里（`[6]`：abs=`3.05e-05`、ULP=1）。
6. **量级**（`[6]`）：六格 rel 差全在 `1.4e-16` 量级，正好是 1 ULP。

### 被实测否掉的假设（这一节的价值主要在这里）

| 假设 | 怎么否的 | 档案 |
|------|---------|------|
| 「CI 装了 pandas 3.0.6，它的解析核给出相邻 double」 | 用 CI 同版本号建 venv，`pd.to_numeric` 六格仍给 `…40997`：**复现 0/6** | `attribution.log` RUN B `[4c]` |
| 「搬运层在 CI 上算出了别的数」 | 同一份搬运层真函数（`stock_financial_report_sina`）在两套栈下各回放一次，六格 `str()` 全是录制值：**0/6** | `cross-env-port-replay.txt`、`cross_env_port_replay.py` |
| 「`requests` 换了 json 后端（simplejson 的 `strtof`）」 | 本机 `requests.compat.json` 就是 simplejson 3.20.2，六个字面量给的是正确舍入值 | `attribution.py` `[4c]` 末段 |
| 「CI 跑的代码/夹具是另一份」 | CI 的 headSha `e2f1c60`：`pd.to_numeric` 那行逐字未变、旧 `_cell` 非数值分支同样是 `str(value)`、夹具文本仍是 `…40997` | `attribution.py` `[4d]` |
| 「窗口/复权之类上游面变了」 | 六格红的是资产负债表数值格，与行情窗口无关；且夹具字节未变 | `[1]` |

**还留着的一条（明写为未闭死）**：CI 是 x86-64 Linux + Python 3.11，pandas 的字符串解析核在
`long double` 上累加，跨平台位型可变；本机是 arm64 macOS（`long double == double`），装不到那个平台。
要在 Linux 容器里跑同一个脚本才能闭死 —— 本机 docker daemon 未启动，起 VM 不在本轮授权范围内，
**这一条不作为结论使用**，只作为下一步。

---

## 3. 暴露面：这类格子全库有多少

同一份响应里的量化面（`attribution.log` `[3]`）：十进制 `item_value` 共 **4,855** 条，其中有效数字
>15 位的 **2,435** 条，按 `%.15g` 走一遍会改变位型的 **39** 条（去重 32 个取值）。
CI 自己的 assert 列表被 pytest 截断过（`[..., ...]` 后面还有），所以「六格」只是打印出来的那部分 ——
真实暴露面就是这个量级。**判据按文本比，这三十九格每一格都是一个随时会复发的红。**

---

## 4. 门禁与测试面

* `make gate` **跑了两遍**，两遍都在文件内读到 `GATE_EXIT=0`：
  RUN 1 `gate-run1-before-backfill.txt`（6,639 行）跑在验收文档 §4 勾选与台账写入之前，
  RUN 2 `gate.txt`（6,645 行）跑在回填之后，所以归档头的 `git status` 与最终 commit 内容同源；
  两遍都完整未裁剪。共同读数：`2918 passed / 6 skipped`、a2-check `A2 files: 315` 四项全 `ok`、
  前端五项含 `frontend-e2e`（`PASS: every declared e2e leaf carries a verdict that can fail.`）。

### 4b. 两遍之间差的那一格：覆盖率读数不是语句级可复现量

两遍的 `TOTAL` 不同 —— RUN 1 `miss=1282 → 86.79%`，RUN 2 `miss=1283 → 86.78%`。
逐文件对账（198 份，只有一份不同）把差异定位到 `opendata/api/data.py`：
Missed 列由 `70, 141` 变成 `70, 140-141`，也就是 `:140` 的 `except Exception as e:` 这一格
在 RUN 1 被走到、在 RUN 2 没被走到。那一行属于 `trigger_download` 里
`asyncio.create_task(_bg_download())` 的后台任务：只有 `execute_download` 抛异常时才会进 `except`，
而抛不抛取决于测试收尾时连接池／事件循环的竞速 ⇒ **这条路径是否被覆盖由并行调度决定**。

本轮不修它（与 C37 的判据面无关），但登记两件事：
①门禁 floor 84%、读数 86.78%，今天这 0.01pp 不改变任何判定；
②**任务 #46（`opendata/pipeline` 抬到 ≥90%）之前，覆盖率面必须先做到可复现** ——
在一个每次跑都会 ±1 条语句的面上抬地板，等于把 C36 那条 83.70% 未达地板的红用另一种方式重演。
两遍日志并列归档就是为了这一点可核对。
* `tests/test_port_fidelity.py`：**31 collected → 25 passed / 6 skipped**，六条 skip 仍全部归属那 4 例
  未补录的 em 夹具（本轮没有为它们做任何放宽）。
* 本轮两份证据脚本自己也在 a2-check 的清扫面里（`docs/evidence/**/*.py`），四项退出码记在
  `a2-compliance.txt` `[6]…[10]` —— 其中一次采集把 `grep` 管道的退出码误记成 bandit 的，已重跑并写明。

## 5. 挣到的一勾

`§4` 条目 3「容忍度配置生效（浮点阈值用例）」翻正（`docs/quality/acceptance-item-ledger.json` 的
`§4|03|b0da1909`，`proven` + 命令 + 档案路径），判据面是 `tolerance-guards.txt` 的 6 passed 与
`mutation-proof.log` 的 M2。台账读数从 `proven=6` 变 **`proven=7 / gap=4 / unreviewed=119 / ticked=7`**。

**AC-6 条目 1 不勾**：它要求的是「P0 域函数与 akshare 原实现做录制回放对照」这一整句，分母里那 4 例
em 用例仍在补录阻塞中（12/16 → 18 例分母下 14 PASS / 4 PENDING，见 C27）。本轮修的是**判定面**，
没有让任何一例 pending 变成 PASS，所以 `条目级 0/3` 保持不动。
**AC-17 条目级也不动（4/10）**：本轮没有把条目 6 的 `opendata/pipeline` 覆盖率抬起（那是任务 #46），
其余条目也没被本轮的绿「顺带」证明。

### 5b. 验收文档回填面（v5.7 → v5.8）

* §0 新增 v5.8 行：判据面（三处改动、`RTOL` 未动）、变异巡回（M1/M2/M3 各抓到什么）、
  七面归因与**被实测否掉的四条假设**、暴露面 4,855／2,435／39、挣到的一勾与不勾的理由、
  以及「不声称 CI 绿」的两条理由。
* §10 `AC-6` 行：追加本轮判定面与 AC-6 原文的对齐过程，状态列的 `条目级 0/3（未逐条达标）` **不动**。
* §10 `AC-17` 行：以「加性更正」的方式续写（含把上一轮末句 `C5…C35 从未 push` 顺延为 `C5…C37`），
  `条目级 4/10` 不动。
* 回填后复算 `python scripts/quality/acceptance_ledger_check.py`：
  `OK: items=130 proven=7 gap=4 unreviewed=119 ticked=7 across 22 group(s) and 19 §10 row(s) reconcile.`
  ——勾选与 `proven` 条目 1:1 对得上（本轮唯一的新勾就是 §4 条目 3）。

## 6. 本轮没做、也不冒充做了的

* **不声称 CI 全绿。** 本轮改动尚未 push（push 需确认），且 CI 目前跑的是 `origin/dev` 上的 `ed32159`
  —— 那上面连 C21 的时区修复、C27 的复权判据都没有。这一条只能等 push 之后用 `gh run view` 复算。
* **没有重录任何夹具、没有改任何用例的阈值来凑数。** `tests/fixtures/upstream/financial_statement/`
  本轮只读未写；`RTOL` 未变；4 例 em 仍 `pending`。
* **依赖未钉死这一层没动。** `.github/workflows/ci.yml` 用 `pip install -e '.[web,dev]'`，
  pandas/numpy 都不锁版本（`ci-env-diff.txt`）：本机绿与 CI 绿之间没有传递关系。
  加锁文件 + 让 CI 装锁是更大的决定，**等确认**，本轮只把事实入库。

## 7. 档案清单

| 文件 | 内容 |
|------|------|
| `attribution.py` / `attribution.log` | 七面归因量测；同一脚本在本机栈与 CI 同装栈各跑一遍（RUN A / RUN B，均 exit=0） |
| `cross_env_port_replay.py` / `cross-env-port-replay.txt` | 搬运层真函数在两套栈下的六格 `str()` 读数（各 0/6 复现） |
| `mutation-proof.sh` / `mutation-proof.log` | base / base-guards / M1 / M2 / M3 / restored 六步，含 sha256 逐字节还原校验 |
| `tolerance-guards.txt` | §4 条目 3 的判据面：6 条用例实名 + 实跑 + `RTOL` 现值 |
| `a2-compliance.txt` | 判据文件、测试文件、两份证据脚本的 ruff/format/mypy/bandit 退出码 |
| `ci-env-diff.txt` | CI 三次 run 的 `Successfully installed` 原文、本机对照、未钉版本的事实入库 |
| `gate.txt` | RUN 2：回填之后的 `make gate` 完整未裁剪输出，`GATE_EXIT=0` 在文件内读取 |
| `gate-run1-before-backfill.txt` | RUN 1：同一批代码、验收文档与台账回填之前的完整门禁输出（两遍并列，不拿前一遍冒充后一遍） |
