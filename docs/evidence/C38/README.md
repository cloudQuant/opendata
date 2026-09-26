# C38 · 让门禁覆盖率读数可复现（C38a），以及它背后那行没人能看的日志

判据来源：`docs/迭代计划/迭代1-重构数据中台/验收文档.md` §4 条目 6（覆盖率地板面）与
AC-17（质量门禁自身可复现）。任务 #47 的口径是：**给后台任务的异常路径加一条确定性用例，
然后连跑两遍覆盖率证明 miss 数稳定；不得改 `exclude_lines` 凑数。**

本轮分两步：C38a（本文件，已完成）与 C38b（`opendata/pipeline` 单元面补到 ≥90%，见 §10）。

## 1. 起点：C37 §4b 留下的那一格

同一份代码在 C37 跑了三遍门禁，覆盖率有两个读数（86.79% / 86.78%），差别全部落在
`opendata/api/data.py` 的 `140-141` 两行。登记时把它当"语句级不可复现"读，本轮按这个口径去做。

## 2. 量到的第一件事不是 flake，是一行没有内容的日志

新用例第一跑就红，红点不在时序，在消息本身：

```
E   AssertionError: assert 'execution 1' in 'Background download failed for execution %s: %s'
```

那行"后台下载失败"的日志从来没带出过执行号和原因。探针读数（`loguru-census.txt` [0]，
命令就是 `python docs/evidence/C38/loguru-format-census.py --probe`，loguru 0.7.3，合成模板、
不是真机日志；探针会把 loguru 默认的 stderr sink 摘掉再量，所以两次运行逐字节相同），
读数是：loguru 用 `str.format` 渲染 —— `%s` 模板配参数
**参数被丢掉、模板原样进日志**，只有 `{}` 会替换；`logging` 恰好相反（`%s` 正确、`{}` 不替换）。
所以"看到 `%s` 就改"也不成立，得先判这个 logger 绑到哪一家。

覆盖率和它是同一个病灶的两个症状：那两行以前从来没被稳定执行过，也就没有人读过它渲染成什么。
本轮改完，`{}` 之后消息是 `Background download failed for execution 1: warehouse unavailable`
（这一条由用例断言，见 §4）。

## 3. 全库普查，以及普查自己的盲区

`loguru-format-census.py`（AST，绑定感知）读数（`loguru-census.txt`）：

| 面 | 读数 |
|----|------|
| [0] 仪器探针（`--probe`，可重跑可 diff） | `%s` 参数不渲染 / `{}` 渲染 / stdlib `%s` 正确 —— 三把尺子同向 |
| [1] 带参数的日志调用（opendata + scripts） | 15 条 |
| [1] 其中参数永不渲染 | **12 条**（本轮修掉的 2 条不含） |
| [1b] 同一条命令跑在 `HEAD:opendata/api/data.py` 上 | 该文件 2 条 DROPPED ⇒ 12 = 14 − 2，**"修前 14" 现在在树上可复算**，不再依赖一次 /tmp 往返 |
| [2] attribute-form 接收者（`self.logger.x(...)`） | 28 条 —— **脚本一条都不判** |
| [2] 该盲区里带 `%s` 参数的 | 1 条（`akshare_provider.py:498`，手读 `self.logger = logger or _default_logger` ⇒ loguru） |
| [3] 留给 C39 的总数 | 12 + 1 = **13 条** |

13 条不在本轮判据面上，且逐条要先读绑定，故登记不顺手改（任务 #49 / C39）。盲区本身进 C39 的
活单：census 判不动的三类面（attribute-form、f-string 已插值、运行时才决定绑定的注入式 logger）
写在 [3] 而不是被"0 findings"盖过去。

## 4. 改了什么

1. `opendata/api/data.py` 两条 loguru 模板从 `%s` 改成 `{}`：`Unhandled exception in background task: {}: {}`（HEAD `:70` → 现在 `:72`）、`Background download failed for execution {}: {}`（HEAD `:141` → 现在 `:143`）
2. `tests/test_api_data_full.py`：+4 例、+2 个本地装置（loguru sink 采集、有界轮询 `_wait_until`）
3. `docs/evidence/C38/loguru-format-census.py`：普查工具，并给它加 `--probe`（把 [0] 那格的四条方向都变成可重跑命令）

用例判的三件事，都是"调用方看不见"的那一半：

- `test_trigger_download_logs_background_failure`：202 已经回给调用方之后才失败的下载，
  仍然要被记下来、要带**同一个执行号**（`worker.await_args.kwargs["execution_id"]` 与
  响应里的 `execution_id` 一致 ⇒ 用户报一个 202 能查到是哪次）、要带原因，
  并且**只记一次**（后台任务不得以未处理异常收尾，否则归因变成 "unhandled task"）。
- `TestLogTaskException` 三例：失败任务给出类型＋原因（整行逐字断言）；取消任务不记，
  且读 `exception()` 必须在 `cancelled()` 过滤之后（顺序反了会从 logger 里抛出去）；
  成功任务不记。

没有动 `exclude_lines`、没加 `pragma: no cover`、没有一条断言是空的。

## 5. 变异巡回（`mutation-proof.txt`，`MUTATION_PROOF_EXIT=0`）

变异按字符串唯一匹配施加（`apply_mutation` 要求 `old` 在文件里恰好出现 1 次，`mutation-proof.py:110-126`），
所以本轮后面那两行注释造成的行号漂移不影响它；跑完按 sha256 还原。

| 步 | 类型 | 变异 | 读数 |
|----|------|------|------|
| base | — | 未变异 | 8 passed |
| M1 | teeth | 下载那条日志退回 `%s`（丢掉执行号与原因） | 1 failed（download 用例） |
| M2 | teeth | 兜底回调那条退回 `%s`（丢掉异常类型与消息） | 1 failed（attributed_by_type_and_reason） |
| M3 | teeth | 取消过滤挪到读异常之后 | 1 failed（cancelled_task_logs_nothing） |
| M4 | **probe** | 把轮询换成裸 `assert failed()` | **1 passed** |
| M5 | teeth | handler 不再走 logger（`print(e)`） | 1 failed |
| M6 | teeth | handler 记完再 `raise` | 1 failed |
| restored | — | — | 两文件 sha256 identical=True |

M4 推翻的是我自己的一句话。设计时写的是"不轮询就不确定"，实测把它换掉后用例仍绿：单文件、
同进程内 `await post()` 已经给了后台任务跑完的机会。所以那条轮询是**防调度抖动的保险**，
不是判据成立的机制。轮询保留（`-n 8` 满载下 M4 那种保证不复存在），但这句话按读数留着，
不改成"轮询使断言有效"。

## 6. 可复现面（`coverage-before-after.txt`）

| | `opendata/api/data.py` | TOTAL 语句/miss | 全局 |
|---|---|---|---|
| C37 run1 | 94 / 2 miss / 96.83%（`70, 141`） | 10942 / 1282 | 86.79% |
| C37 run2 | 94 / 3 miss / 96.03%（`70, 140-141`） | 10942 / 1283 | 86.78% |
| C37 run3 | 94 / 3 miss / 96.03% | 10942 / 1283 | 86.78% |
| 本轮 run1 | 94 / **0** / 99.21% | 10942 / 1280 | 86.81% |
| 本轮 run2 | 94 / **0** / 99.21% | 10942 / 1280 | 86.81% |
| 本轮门禁（第三遍） | 94 / **0** / 99.21% | 10942 / 1280 | 86.81% |
| 本轮最终树门禁（第四遍） | 94 / **0** / 99.21% | 10942 / 1280 | 86.81% |
| v5.9 回填后门禁（第五遍） | 94 / **0** / 99.21% | 10942 / 1280 | 86.81% |

不止那一行：**整张覆盖率表**在两遍 test-cov 之间 `table_diff_lines=0`，门禁那一遍与 run1
`identical=YES`；三遍门禁的表（各 201 行）两两 `diff=0`。被测面口径核对过：
`[tool.coverage.run] source = ["opendata", "opendata_fuyao"]`，
而 C37 三遍之间的改动只在 `docs/` 与 `scripts/quality/`（不在度量面内），所以三遍 TOTAL 语句数
同为 10942 —— 那一格的差异确实只来自那两行。

**为什么要第四遍与第五遍**：`a2-check` 的清扫面含 `docs/evidence/**/*.py`，而 census 脚本在第三遍之后
被改过（加 `--probe`）⇒「门禁绿」描述的是跑它那一刻的树；第四遍之后验收文档被回填（v5.8→v5.9），
而 `ledger-check`／`brand-check` 两个成员的输入就是那份文档 ⇒ 第五遍。文档定稿后的字句更正不再触发
第六遍，改为**逐成员复算**并在 `coverage-before-after.txt` 末节按这一条口径登记边界。

**不写成"已证确定"**：两遍相同不排除低频抖动，五遍也只是把"同一份被测代码给出两个门禁读数"
这一形态消掉成"五遍一个读数"。

## 7. 门禁（`gate-c38a.txt` ＋ `gate-c38a-final.txt` ＋ `gate-c38a-post-backfill.txt`）

第一遍：6,645 行完整未裁剪，14 个成员 + `PASSED` 共 15 个标记，`GATE_EXIT=0` 读自文件内部；
`2922 passed / 6 skipped`（比 C37 多 4 条 = 本轮新增），`Required test coverage of 84% reached`，
secret-check `no leaks found`，`A2 files: 318`（C37 为 315，本轮两份证据脚本进了清扫面）。

第四遍（最终代码树，17:29:08 起跑）：6,649 行、15 个标记、`GATE_EXIT=0` 仍读自文件内部
（后台任务的退出码不参与判定）、`2922 passed／6 skipped in 56.66s`、`86.81%`、`A2 files: 318`、
quality-ratchet `OK: quality debt did not increase.`、public-api 522 个 callable 双 100%。

第五遍（v5.9 回填之后，17:37:50 起跑）：6,647 行、同样 15 个标记逐个在位、`GATE_EXIT=0`、
`2922 passed／6 skipped in 57.89s`、`opendata/api/data.py 94 0 32 1 99.21%`、`TOTAL 10942 1280 2692 298 86.81%`、
`A2 files: 318`、ratchet `OK`、public-api `100.0% (522/522)`、secret `no leaks found`、
ledger `census: items=130 proven=7 gap=4 unreviewed=119 ticked=7`。
三遍门禁的整张覆盖率表两两 `diff=0` ⇒ 两遍之间的改动只落在声明面与档案面，没有挪动判据面。

## 8. 判据合规（`a2-compliance.txt`）

`a2_check --files` 对本轮 4 个 .py 全过（`A2_EXIT=0`，[G] 段可重跑），四个工具逐条复算在 [G2]。
**第一跑红过四项**，全部是本轮自己引入的，逐条按根因修（[R] 段留了当时的读数）：

* `from __future__ import annotations` 一加，只作注解用的 `AsyncClient`／`AsyncSession` 就撞上
  TC002 —— 本轮不需要那句 `__future__`，删掉，而不是给两行加 `# noqa`；
* `ruff format` 要求重排测试文件 —— 跑 format，不动 line-length；
* census 里 `node.args[index].value`：mypy 不收窄下标 → 先绑局部变量再 `isinstance`；
* `import subprocess` 的 B404 加 `# nosec B404`，并且**留了账**：bandit 的 `skipped_tests` 从 1 变 2、
  `results` 从 1 条变 0 条（两个数字都在 [G2]/[R] 里，不是"豁免了就看不见了"）。

给 census 加 `--probe` 之后又撞出三条同类 mypy 错（`logger.Logger`、`logger.__version__`、
`typing.override`）。前两条 mypy 先说对、运行时才读出来（真的抛 `AttributeError`）；
第三条只在类型面：本机跑 3.13，`typing.override` 运行期存在，是 mypy 按 `python_version = "3.10"` 判它不存在。
所以它的修法值得记一句：没有用 `typing.override` 去压 N802，而是改成不子类化 `Handler`、
直接捕获 stderr 读数 —— N802 随成因一起消失。
**没有任何一项靠改配置、加例外或放宽规则关掉。**

[R] 段那份 bandit JSON 的 metrics 抄了字段子集（省掉重复的 0 值列），results 那一条去掉了五个排版字段：
计数读数完整保留，但"完整未裁剪"这句话在这一格上不打满，档案里已按这一句登记。

## 9. 登记，不声称

* 台账本轮**不翻任何条目**：AC-17 条目 6（≥90%）要等 C38b；AC-16 条目级仍 2/9，AC-17 仍 4/10。
* `opendata/api/data.py` 的 99.21% 还剩一格分支（`189->191`），本轮不追，也不为它写空断言。
* 那 13 条 loguru 参数丢掉的调用**没修**，理由不是"不重要"，是它不在本轮判据面上且逐条要读绑定。
* 旧那条 happy-path 用例（`with patch(...)` 一 POST 完就退出）的窗口问题是这格竞态的**机制解释**：
  新用例把所有断言留在窗口内，并把"工作线程在窗口内被 await"变成断言。但本轮**没有**直接量到
  "真 `execute_download` 被调到过"（那要放一个会写库的假失败才能量，本轮不放）。这一格是推理，
  档案里那两个不同读数才是实测。
* 本轮全程只读数仓（用例里 `execute_download` 是 mock），未执行任何 DDL／写入，未读取或打印任何密钥值。

## 10. 档案清单

| 文件 | 内容 |
|------|------|
| `loguru-format-census.py` | 绑定感知的 AST 普查工具，`--probe` 是那格前提的实测面 |
| `loguru-census.txt` | [0] 探针（四个方向全实测）→ [0b] 库源码那九行 → [1] 修后普查（12/15）→ [1b] "改前 14" 改从 git 对象复算 → [2] 它自己的盲区（28 条 attribute-form，1 条带 `%s`）→ [3] totals 与未判面。每段 `$` 行即可重跑命令 |
| `mutation-proof.py` / `mutation-proof.txt` | base + 5 teeth + 1 probe + sha256 还原校验，`MUTATION_PROOF_EXIT=0` |
| `stability-run1.txt` / `stability-run2.txt` | 门禁自己的 `test-cov` 命令连跑两遍，完整未裁剪（各 6,087 行） |
| `coverage-before-after.txt` | C37 三遍 vs 本轮五遍的同表对照；整表 diff=0 / identical=YES / 三遍门禁两两 diff=0，末节写"为什么不再跑第六遍" |
| `gate-c38a.txt` / `gate-c38a-final.txt` / `gate-c38a-post-backfill.txt` | 三遍 `make gate` 完整输出（6,645／6,649／6,647 行）：第一遍跑在 census 脚本被改之前，第二遍是最终代码树，第三遍是 v5.9 回填之后的树（见 §7） |
| `a2-compliance.txt` | [G] 终态 a2_check + [G2] 逐工具复算 + [T] 代码面未动的 sha 证明 + [R] 首跑四条红的原样读数 |
