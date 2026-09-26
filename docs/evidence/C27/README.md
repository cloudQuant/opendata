# C27 轮：把一条恒真的复权判据换成三条能红的（task #35，收编 task #14 的 em 挂账）

日期：2026-09-26｜分支：`dev`｜前置：C26（`aec3088` 适配层静默丢弃审计）
本轮的入口任务是「补录 em 通道那 4 个挂起夹具，让 AC-6 到 16/16、门禁 5 条 skip 归零」。
em 通道到本轮结束**仍然一个都没录上**（10 轮补录驱动跑满，4 例 em 到末轮仍 `pending`；`refusal-shape.txt` 量清了它今天被拒的形状），
但顺着这条任务去读那批夹具能证明什么，量出来一件比补录更要紧的事：
D10 那条复权判据**永远不会失败**，而且它以同一形状活在**两个地方**。
所以下面这些读数是本轮真正的交付：一条恒真判据被删掉，换成三条各自可证伪的，
并用一次变异巡回证明它们**抓的是互不重叠的三类错**。

---

## 1. 缺口从哪来：一条判据为什么能挂在 skip 上半年

A1 留下的那条断言是：`factor = qfq_close / raw_close`，然后断言 `raw * factor == qfq_close`。
`apply_adjust` 干的就是 `close * ratio`（`opendata/data/adjust.py:66`），
所以除了「乘法坏了」它没有别的失败模式——判据的**期望值**和**被测值**是同一个数。

| 位置 | 形状 | 为什么一直没被读到 |
|------|------|-------------------|
| `tests/test_port_fidelity.py::test_d10_qfq_synthesis_matches_official_series` | 上面那条，且**只看 em 那一对夹具** | em 从 A1 起一次都没录上 ⇒ 这条用例自诞生起**每次门禁都 SKIPPED**（C26 那 5 条 skip 里就是它）。一条永远不执行的判据不会红，也不会有人去重读它 |
| `scripts/codemod/compare_with_upstream.py::_check_d10_qfq` | **同一套算术的第二份拷贝**，也只看 em 那一对 | em 缺席时它返回 `failed: False` + 一句 reason（「改由 `scripts/ops/qfq_official_check.py` 跨源核」）⇒ 报告那一节**只有一句解释、没有任何读数**，读者分不清「复核过且没问题」与「今天没法复核」 |

「每次门禁都 SKIPPED」不是修辞，是从留档里数出来的（`d10-skip-history.txt`）：本轮之前 `docs/evidence/` 下
**32 份**未裁剪门禁/pytest 原始输出含这条用例的判决行，**32 次全部写着 `SKIPPED`，`PASSED` 与 `FAILED` 各 0 次**。
数的是转写里真实出现过的判决行，不是从「夹具一直 pending」推出来的。

⇒ 两处合起来的实际效果：**「D10 复权链已复核」这句话从被写下那天起，一次也没有基于读数说出来过**，
而即使 em 明天录上，它跑到的也是一条恒真断言。所以本轮先改判据再谈补录：
**补上一个窗口，也补不出一个不会失败的判据能证明的东西。**
（第二处不在原任务书里，是把判据搬进共享模块、回头重写 `--compare` 那一节时撞见的。）

## 2. 判据底稿：噪声地板、台阶、以及「窗口放宽」这件事本身可不可信

`qfq-chain-measure.py` 是**独立第二实现**（故意不 import 被测模块），只读 `tests/fixtures/upstream/`，
零网络。它先量「夹具能说什么」，再决定判据写什么：

| 段 | 量什么 | 读数 |
|----|--------|------|
| A | 同一 vendor 内，复权比例按 open/high/low/close 分别算，四个比值该一致（复权是标量乘数） | sina 窄窗最大相对差 `6.117e-06`、宽窗（118 天）`6.251e-06` |
| B | 跨 vendor `f_em/f_sina` 是否为常数 | **未判读**（em 夹具缺，不下结论） |
| C | 因子链在本窗口内**有没有台阶** | 窄窗 `整窗恒定：A/B 判据在此退化`；宽窗 `台阶日 {'2024-06-19': 0.0207102910…}` |
| D | 两家**不复权**收盘逐位相等 ⇒ 两帧真的说的是同一个标的（AC-6 的结构性盲区：两家都取错标的时回放照样 PASS） | **未判读**（em 缺） |
| E | 台阶的**日期与高度**是否等于另一只端点（分红明细）算出来的 `P_prev/(P_prev−cash)` | 宽窗 `前收 1521.5 每股现金 30.876 → predicted=1.0207134730 actual=1.0207102910 相对差 3.117e-06 OK`；窄窗 `窗口内既没有台阶也没有应出现台阶的分红日——本节无判据可言` |
| F | 放宽窗口前后是否还是同一条链（本轮改动的前提条件） | 重叠 22 天**逐位相等 22/22**；`q(t)=f_宽/f_窄` 中位数 `1.000000`、最大相对偏离 `0.000e+00` |

F 段那条读数是本轮最容易被跳过、也最该留档的一句：**「把窗口放宽」本身是一个可证伪的前提，不是免费的操作。**
如果 sina 的前复权锚定随请求区间移动，那么跨窗口的两条链就不是同一条链的截取，
E 段量到的吻合也只能是巧合。实测它不随窗口移动（偏离恰好为 0），放宽窗口才成立。

阈值都是量出来的，不是拍的：无台阶窗口的相邻日抖动中位数 `1.426e-06`／最大 `5.453e-06`，
真实除权台阶 `2.071e-02`——差三个数量级，`1e-4` 落在中间的空白带里（`STEP_FLOOR`/`ADJUST_TOLERANCE` 的注释就写着这两个数）。

## 3. 三条判据各管一段：覆盖面不重叠是量出来的，不是声明的

`scripts/codemod/qfq_chain_checks.py` 三条判据 + 一台巡回（`judge-falsification.py`）：

| 坏数据（只在内存里构造，夹具一字节未动） | 台阶判据 | 合成判据 | 窗口自洽 |
|------------------------------------------|---------|---------|---------|
| `unrelated`（整条链换成不相关的数） | FAIL | FAIL | FAIL |
| `step_delayed`（台阶挪到 6-20） | FAIL | FAIL | PASS |
| `wrong_height`（台阶高度错 0.5%） | FAIL | FAIL | PASS |
| `one_field`（只错 `high`） | PASS | FAIL | PASS |
| `cash_10x` / `cash_removed` / `date_shifted`（改**事件流**而非价格） | FAIL | — | — |
| `overlap_close_off_by_a_cent` / `overlap_factor_moved`（改**另一次录制**） | — | — | FAIL |
| `uniform` / `uniform_anchor`（只换前复权锚定常数） | PASS | PASS | PASS |
| `untouched`（对照组） | PASS | PASS | PASS |

三张读法：
1. **`one_field` 只有合成判据抓得到** ⇒ 「三条」不是把一条判据换了三个名字。台阶判据看的是 close 的相邻比，
   `high` 单独错它天然看不见；反过来 `step_delayed` 的扰动落在 6 月、重叠区只到 1 月，窗口自洽判据管不着。
2. **`uniform` 必须全 PASS**：等比换锚不改变任何相对形状，被抓到反而说明某条判据把「这一家的锚定」当成了事实。
   本轮把这条写成**假阳性守卫**（误抓即 `JUDGE_FALSIFIED_EXIT=1`），因为判据太宽会漏、太窄会冤枉合法实现。
3. **旧判据在 5 种坏收盘数据上 PASS 了 5 次**（巡回第 1 段），这就是它挂在 skip 上半年没人发现的原因。

## 4. 机制：一份代码，三个地方跑

| 消费方 | 用法 |
|--------|------|
| 门禁 `tests/test_port_fidelity.py` | 2 条判据 × 2 个 vendor 对 + 1 条窗口自洽；断言写成 `diffs = checks.check_*(); assert not diffs` |
| AC-6 报告 `compare_with_upstream.py --compare` | `_check_d10_qfq` 改为调用同一份模块，报告新增 `## D10 qfq factor chain (checks shared with the gate)` 一节 |
| 巡回 `judge-falsification.py` | 直接 import 同一份模块装坏数据，不 monkeypatch ⇒ 巡回证明的判据就是门禁跑的判据 |

删掉的不是「一份重复代码」而是**同一事实的两处独立陈述**：判定算术从测试文件与报告生成器各搬一份出来、
合成一个模块（`qfq_chain_checks.py` 326 行），测试文件里两处因此只剩 `diffs = checks.check_*()` 三行调用，
腾出的篇幅挂上了第三条判据。
报告这一节的现读数是 `em: 未判读（夹具还没录到，本轮没有回放这一对）` / `sina(宽): PASS（118 天）` /
`重叠 22 天…锚定比…最大相对偏离 0.000e+00`——**未判读与通过分开写**，这是 `_check_d10_qfq` 改造前后最大的行为差别。

`_check_d10_qfq` 改造前还有一处更隐蔽的行为：它对「夹具没录到」返回 `failed: False` 加一句 reason，
而不是把这一对列成未判读——`failed` 是报告里唯一区分「过 / 不过」的字段，
于是**缺席在档案里读起来像通过**（AC-6 那份报告至今有 4 个 PENDING 用例，说明这个形状不是本轮才有的）。
现在它打印的是 `em: 未判读（夹具还没录到，本轮没有回放这一对）`，与 `sina(宽): PASS（118 天）` 并列在同一节里。

## 5. 明确拒绝的四种「让它过去」

1. **把 em 从 `ADJUST_PAIRS` 里摘掉来消 skip**。摘掉之后 skip 数好看一条，代价是 em 补录成功那天没人记得把它加回来——
   那是把「量不到」伪装成「不需要量」。现设计是 fail-closed 占位：夹具一 recorded，两条 em 参数立刻自己开跑。
   同一条理由管着另一处改动：`stock_daily_raw`/`stock_daily_qfq` 两个 CASES 的 `end_date` 从 `20240331` 推到 `20240701`
   （把窗口挪到跨过除权日），**夹具本身一份都没重录**（它们还没有夹具，skip reason 里那条 URL 就是这条准备的读数）。
2. **挪时区、换 secid、改窗口，重录一份「能过」的夹具来凑数**。em 今天的形状是 `push2delay` 502 + `push2his`
   `ConnectionError`（`refusal-shape.txt` A 节，直连无重试），与代码无关，重录不改变事实。
   本轮确实改了窗口，但改的是**还没有夹具的那两个 em 用例的参数**（§5-1），不是在已有夹具上试到它过为止。
3. **把 `index_daily_em` 的 `EmptyReferenceFrame: upstream returned 0 rows` 读成「这个指数没有历史」**。
   同一时刻 pristine 上游返回 0 行、搬运层（C11a 改过的那条）抛错——两棵树面对同一个网络状态的差别
   正是 AC-5/C11a 那次修的「吞错」缺陷的现场形状（`refusal-shape.txt` B/C 段）。**被拒 ≠ 确实没数据。**
4. **用 `round(v, 10)` 数「取值种类」来判断有没有台阶**。首版度量脚本就是这么写的，于是把 `1e-6` 舍入噪声
   数成「22 种取值 = 有台阶」，对不含除权日的窄窗给出『窗口内含除权日』的假结论——**又一条永远不会失败的判据**。
   改成相邻日抖动阈值后同一段窄窗打印为『整窗恒定』。宽窗那一条把这件事讲得最清楚：
   `取值 114 种` 而 `台阶日（抖动 > 1e-04）：{'2024-06-19': …}` 只有 **1 处**——
   113 种是两位小数报价的舍入噪声，把它们数成事件就会得出「这条链几乎天天除权」。
   这一处自我纠错留在 `qfq-chain-noise-floor.txt` 抬头，两条读数在 `qfq-chain-final.txt` 的 C 段。

还有一处「未判读 → 别处已验」的替代写法本轮被删：旧 `_check_d10_qfq` 在 em 夹具缺席时把判据**转嫁给另一个脚本**
（reason 原文：`the adjustment claim is checked cross-source instead by scripts/ops/qfq_official_check.py`）。
`qfq_official_check.py` 本身仍然有效、仍服务 AC-11 的**服务端合成 vs 官方 sina 序列**那一面（B4 起的真机证据），
但它答的是另一个问题——它需要网络与数仓，且核的是「服务层合成出来的价」而不是「夹具里那条链的台阶日期与高度」。
夹具缺席时报告现在只说 `未判读（夹具还没录到，本轮没有回放这一对）`，不再顺手把这一格算成已复核。

## 6. 证据清单

| 文件 | 内容 | 判据 / 退出码 |
|------|------|-------------|
| `em-channel-measure.py` / `.txt` | **修复前基线**：em 候选 secid 的身份读数、生产调用两次比对、`index_code_id_map_em` 命中面 | 全部**未判读**（`ConnectionError`／4 个 secid 全 502），`MEASURE_EXIT=0`（未判读不计为数据错） |
| `refusal-shape.py` / `.txt` | 同一网络状态下 pristine 上游 vs 搬运层各自怎么处理「被拒」 | `upstream: 返回 0 行` / `ported: 抛 RuntimeError`，`REFUSAL_SHAPE_EXIT=0` |
| `record-em-fixtures.sh` / `record-em-fixtures.txt` | 10 轮 × 6 例补录现场档（`MAX_ROUNDS=10 SLEEP_SECONDS=70`；每例补录前后各读一次 `meta.json` 状态，调用间节流 70 s） | `RECORD_EXIT=1`／`DRIVER_RC=0`：4 例 em 至末轮仍 `pending`，两份 sina 宽窗夹具 `recorded` 未被触碰。档中另**如实记下第三种拒绝形状**——round 10 的 `stock_daily_raw` 不是被拒而是**阻塞**（墙钟 7 分钟、CPU 0.55 s、一条 ESTABLISHED socket），处置是终止该子进程让驱动继续，并在档尾补记（不改写上面任何一行） |
| `d10-skip-history.txt`（61 行） | 把 §1 那句「这条判据自诞生起每次门禁都 SKIPPED」变成可复算的计数：正则扫 `docs/evidence/**/*.txt` 里该用例的判决行 | **32 份留档输出、`SKIPPED` 32 次、`PASSED` 0 次、`FAILED` 0 次**（判据「从没执行过」不是推断） |
| `tests/fixtures/upstream/stock_daily_sina_{raw,qfq}_wide/` | **本轮新录的两份夹具**（600519，2024-01-01..2024-07-01，118 个交易日，跨 2024-06-19） | `--record` 写入 `meta.json status=recorded`；`--compare` 离线回放两者 PASS（0 单元格差异），AC-6 用例数 16 → **18** |
| `qfq-chain-measure.py` / `qfq-chain-noise-floor.txt` / `qfq-chain-final.txt`（53 行） | 独立第二实现，A–F 段读数（§2 表） | 首跑（宽窗夹具还没录到）`QFQ_CHAIN_EXIT=1`；最终跑仍 `=1`，**原因只是 em 未录全**（脚本对「什么都没量到」fail-closed），两条退出码都不是判据失败 |
| `judge-falsification.py` / `.txt`（90 行） | 旧判据在 5 种坏数据上全 PASS、新判据逐种表现、事件流与窗口侧扰动、对照组、假阳性守卫 | `未被任何新判据抓到的坏数据 = 无`、`被误抓的合法扰动 = 无`、`JUDGE_FALSIFIED_EXIT=0` |
| `compare-d10-final.txt`（53 行） | `--compare` 离线复算（零网络，应答全来自转写）＋**报告新增那一节的逐字快照**（`--compare` 的 stdout 只有逐用例行，D10 读数只落在报告文件里，故另存） | 回放 **14 PASS / 4 PENDING / 0 差异**（C26 是 12/16：本轮新录的两份宽窗夹具都 PASS）；`COMPARE_RC=1` 是「仍有 pending 用例」的既有 fail-closed 口径，不是差异 |
| `gate.txt`（6,064 行，未裁剪） | `make gate` 全量原始输出，抬头带采集时间／分支／HEAD／`git status --porcelain`／Python 与 node 版本 | `GATE_EXIT=0`（读自文件倒数第 1 行，非后台任务退出码）：`2676 passed, 6 skipped in 51.12s`、覆盖率 86.49%、前端 8 files / 79 tests |

补录驱动动过工作树，但动的不是数据：4 份 em `meta.json` 的 diff 只有 `recorded_at` 与 `reason` 两行
（`status` 仍为 `pending`，`reference.csv.gz`/`responses.json.gz` 一字节未写），另两份 em 用例的 `params.end_date`
随行内 `reason` 一起从 `20240331` 更新为 `20240701`（那是本轮 CASES 改动在转写里的镜像，不是新数据）。
拒绝形状本身换了：`stock_daily_raw` 的 reason 从「DNS 解析不了 `push2delay`」变成了「502 Bad Gateway」，
两种写法都留在档里，读的是同一件事——**这个用例今天仍然没有夹具**。

## 7. 过程中被推翻的四处设计（不遮掩）

1. **任务书本体被实测否掉**。原计划「补录 em ⇒ AC-6 16/16」在动手前先跑了一次通道测量
   （`em-channel-measure.txt`），4 个候选 secid 全 502／连接被断，于是补录改写成「带节流、可重跑的多轮驱动」
   并把判据面从「补齐 em」移到「把已录到的东西说对」。
   **本轮没有让任何一条 em 记录变成 PASS**，也没有因为补不上而把 AC-6 计数改成「16 例不适用」。
2. **巡回自己抓到自己两处脚手架假绿**（都是先跑出来才发现，不是想到才改的）：
   **a** 第 1 段循环装 5 种坏数据，收尾断言却写死 `survived == 4`，打印出「PASS 了 5/4 次」；
   **b** 更严重——两条窗口变异**当场 PASS**：一个原因缺陷被放在第 ≥59 行（22 天重叠区之外），
   一个原因是那次扰动是**等比** 1.001，恰好是本轮亲手定义为「合法」的换锚形状。
   处置不是把阈值调严，而是把变异挪进重叠区、并把等比那一例正式改写成 `WINDOW_TOLERATED` + 假阳性守卫。
   一条会把合法实现判为缺陷的判据，和一条恒真判据是同一枚硬币的两面。
3. **`_check_d10_qfq` 是计划外发现的第二份拷贝**。最初只打算改测试文件；改完后 `--compare` 报告里那节仍写着旧的
   「`factor = qfq_close / raw_close` 再乘回去」那套算术，才顺着 `ADJUST_PAIRS` 找到它。
   它不是打印一个自己算给自己的 PASS——那还不如它实际的样子：em 夹具缺席时它返回 `failed: False` 加一句
   「这个claim 由 `scripts/ops/qfq_official_check.py` 跨源核」，于是报告里那一格**既没有读数、又长得像已复核**。
   如果只做测试那半边，本轮结论就只成立一半：门禁不再跑恒真判据，档案里那句「D10 复权链已复核」却仍然没有读数支撑。
4. **度量脚本与判据同源的风险被明确保留为两份实现**：`qfq-chain-measure.py` 故意不 import `qfq_chain_checks`，
   代价是同一事实有两套代码；换来的是「F 段说窗口无关」与「判据说窗口无关」互为独立对照。
   C26 的教训（脚本自己复算单位 ⇒ 永远停在旧结论）在这轮是反向应用的：**测量面可以独立，判定面必须唯一。**

## 8. 门禁与测试数字

| 面 | C26 基线 | C27 | 说明 |
|----|---------|-----|------|
| `make gate` | `GATE_EXIT=0` | **`GATE_EXIT=0`**（`gate.txt` 第 6,064 行） | 全量未裁剪，抬头记录 branch/HEAD/porcelain/Python/node |
| `pytest -n 8 -m "not e2e"` | 2,671 passed / 5 skipped（50.11s） | **2,676 passed / 6 skipped（51.12s）** | collected 2,682：净 +6、passed 净 +5、skip 净 +1，逐条对账见下方与 §9 第 1 条 |
| 覆盖率（阈值 84% 未动） | 86.51%、TOTAL 10,730 / 未覆 1,278 | **86.49%、TOTAL 10,730 / 未覆 1,280** | `opendata/` 一字节未改（`git status --porcelain opendata/` 为空）；197 行覆盖表里**只有一行不同**，见下面「覆盖率动了 0.02 个百分点」 || `tests/test_port_fidelity.py` 单跑 | 14 passed / 5 skipped（19 collected） | **19 passed / 6 skipped（25 collected）** | collected +6 = 宽窗两例新夹具 + 两判据 × 两 vendor + 窗口自洽 − 删掉的恒真那条；全库那 6 条 skip 全部来自这个文件 |
| a2-check | 269 文件 | **274 文件** | 逐名对账：新增 `scripts/codemod/qfq_chain_checks.py` + 4 个证据脚本，**删除 0 个**；274 条全部过 ruff+format+mypy+bandit |
| public API | 450/450 | **459/459（docstring 100%、annotation 100%）** | `scripts/quality/public_api.py` 的扫描面**含 `scripts/codemod`**（该文件第 37 行），所以判据模块的 9 个公共函数是**计入公共 API 面的**，不是豁免区 |
| 质量棘轮 | `ruff_selfdev 274`／`ruff_ported 2144`／`direct_http_ported 1044`／`mypy 21`／`bandit 4` | **五项与 C26 逐条相等**（snapshot 277/2145/1045/21/4，三条 improved 未 `--update` 冻结） | 本轮新增文件全在 A2 严格集内，贡献 0 新债；沿用「只降不升、不 `--update` 冻结」 |
| 前端 | 8 files / 79 tests | 8 files / **79 passed**（3.11s） | 本轮无前端改动，门禁仍照跑 |

`skip 数 +1` 的逐条归属（读自 `gate.txt`，不是从用例名推的）：
4 条 `test_ported_output_matches_upstream[stock_daily_raw\|stock_daily_qfq\|index_daily_em\|fund_etf_daily_em]`
+ 2 条新判据的 `[em]` 参数（`test_qfq_factor_steps_are_the_recorded_ex_dates[em]`、
`test_qfq_synthesis_reproduces_the_official_series[em]`）。C26 那 5 条 = 前 4 条 + 1 条
`test_d10_qfq_synthesis_matches_official_series`；本轮删掉恒真的 D10 那条（−1），它的位置换成 `[em]` 两条参数（+2），
根因与那 4 条**完全相同**（`push2delay` 502 / `EmptyReferenceFrame`），skip reason 里带的就是录制失败原文。
⇒ 净 +1 条 skip，换来 3 条真在跑的判据（sina 两条参数 + 窗口自洽那条全绿）；这不是新挂账，是同一笔账换了一种**能红**的记法。

覆盖率动了 0.02 个百分点，方向还是「变差」，所以本轮把它钉到行：197 个文件里**唯一变化的行是
`opendata/api/data.py`**（未覆 2→4、部分分支 2→3，缺的行是 67 与 140-141，即后台任务
`task.exception()` 与背景下载 `except Exception` 两条异常路径），而这个文件本轮没有被修改、也不在 diff 里。
把 C4–C27 的同一行拉出来数（26 份门禁留档、本轮除外），它已经有**四种读数**：
`70, 141, 187->189` 21 次、**与本轮完全同形的 `67, 70, 140-141, 187->189` 2 次（C6、C13）**、
`67, 70, 187->189` 2 次（C10、C15）、`70, 140-141, 187->189` 1 次（C12）。
⇒ 这是覆盖面上一个既有的**时序依赖观测**（后台任务是否赶在 TestClient 拆除之前抛错／被取消），
不是本轮的回归；本轮没有为它调阈值、也没有把它写成「覆盖率下降」，只登记为遗留（§10 第 7 条）。
方向上也得说清：本轮把同一套判定从「两份恒真算术」换成「三条可证伪判据」，覆盖率**降** 0.02 个百分点
不是因为新代码没被跑到（新夹具与三条判据都在门禁里真跑），而只是这一行的抖动——把抖动当成趋势，是另一条恒真判据的诞生方式。

## 9. 判据未放宽的声明

1. **em 一条都没算成通过**：三处独立说「未判读」（pytest skip reason 带录制失败原文、度量脚本 B/D 段、AC-6 报告新节），
   没有一处读成 PASS 或读成「数据错」。
2. **AC-6 的分母是被加大的，不是被达标的**：用例数 16 → **18**，通过 12 → **14**，pending 仍是 **4 条**（都是 em）。
   新增的 2 例通过全部来自本轮新录的 sina 宽窗夹具，挂账仍是 task #14；本轮把「补录」从目标降级为「可随时重跑的驱动 + 现场形状档」。
3. **窗口放宽这个前提被自己钉住**：新增的 `test_widened_fixture_window_describes_the_same_series` 是门禁用例，
   不是脚本里的 printf——下一轮谁再放宽窗口，它会先红。
4. **没有加豁免、没有缩面**：`pyproject.toml`、`bandit.yaml`、`docs/quality/`、`scripts/quality/` 的 `git status` 为空；
   删掉的那 1 条用例（`test_d10_qfq_synthesis_matches_official_series`）是恒真判据本身，
   替代它的是 3 条各有独立失败模式的判据（§3 表里它们的红面互不重叠），断言数量没有减少反而增多。
5. **巡回的退出码是复合的**：`survived == len(CORRUPTIONS)+1`（旧判据全放过）**且** 每种坏数据至少被一条新判据抓到
   **且** 三条对照组全 PASS **且** 合法换锚零误抓 **且** 无 ERROR——四半缺一即红。

## 10. 遗留（明确不做 / 做不了，含原因）

1. **em 通道 4 例仍挂起**（task #14）。本轮把「为什么」量清了（`refusal-shape.txt`：`push2his` 连接被远端断、
   `push2delay` 502 非 JSON），也留了可重跑驱动；但**判据面已经不再等它**——sina 那一侧现在能独立证伪。
2. **`ADJUST_PAIRS` 只有 sina 一对有读数**。em 那一对今天的作用是占位 + 报告里的未判读行；
   跨 vendor 的 B 段（`f_em/f_sina` 是否为常数）要到 em 录上那天才有第一次真读数。
3. **D 段（标的同一性）本轮设计出来了但没跑成**：它需要两家同一天的不复权收盘，em 缺 ⇒ 未判读。
   这条不是装饰——它是 AC-6 结构性盲区（回放只比「搬运层 vs 上游对同一份转写的返回」，两家都取错标的照样 PASS）
   目前唯一能堵上的判据，故**留在度量脚本里而不是删掉**。
4. **前复权锚定常数按 vendor 各有一份**，本轮判据全部只读相对量（相邻日比值、两窗比值），
   所以「哪家锚在哪一天」不参与判定。真要跨家比绝对复权价，得先立锚定日口径，属迭代 2 的账。
5. **`qfq_chain_checks` 不在 `--cov` 统计面内，但在公共 API 面内**：`pyproject.toml:277` 的
   `source = ["opendata", "opendata_fuyao"]` 决定了它不计覆盖率，而 `scripts/quality/public_api.py:37` 把
   `scripts/codemod` 算进 A2 公共 API 扫描面——所以它的 9 个公共函数必须带 docstring 与类型标注（门禁实测 459/459 100%）。
   本轮判据的正确性由巡回（内存装坏数据）与门禁（真夹具）双向保证；若要把覆盖率统计扩到 `scripts/codemod`，那是与 C7 同类的独立决策。
6. **`compare-report.md` 是滚动活档**，每次 `--compare` 都重写；本轮读数已另存 `compare-d10-final.txt`，
   别把它当不可变证据引用。
7. **覆盖面上 `opendata/api/data.py` 的两条后台任务异常路径（67、140-141）本轮登记为既有不稳定观测**：
   同一行在 C4–C26 的 26 份门禁留档里已有**四种读数**（21/2/2/1 次，其中两次与本轮完全同形：C6、C13），
   而本轮该文件未被修改、也不在 diff 里。要收它得让那两条路径变成**确定性可测**
   （显式触发一次 task 异常与一次背景下载异常），那是测试面的一次独立改动，塞进本轮只会把「判据可证伪」这条主线稀释。
