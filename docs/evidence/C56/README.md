# C56 —— AC-11\|02 的官方对照腿：把「脚本自报的腿名」做成四条交叉读数，然后把那次 FAIL 归因到复权口径

任务 #72。判据原文：**`adjust=qfq|hfq` 服务端计算**：同一标的 qfq 序列与 akshare 官方 qfq 对照一致（容忍度内）。
本轮结论先说：对照腿这一半从「不可验」变成「可验且已过」，判据的另一半（**一致**）量出来是 **FAIL**，
所以 `AC-11|02` 仍是 gap，条目级 4/12 不变，容差 2e-3 与逐日 shape dev 的判据口径一字未改。

读数分层：**〔档案〕**=本轮留下的未裁剪正文里能 grep 到的字面读数；**〔源码〕**=探针当场从代码里解析出来的结构读数；
**〔推断〕**=由前两类读数推出的判断，标清楚是谁推的、被什么证伪。

## 1. 上一轮的读法为什么不算证据

C55 的判据面读的是仪器自报：`_official_series` 里第一个 `stock_zh_a_*` 出现什么名字，就认为对照走的是那条链。
有了分派表之后那个 capture 只是「分派表点名的模块」的代理 —— 一个字符串可以自称 akshare 而什么都不解析到它。
所以本轮把「走的是哪条官方链」拆成四条互相独立的读数〔源码〕：

| 读数 | 取自 | 现值 |
|---|---|---|
| `default_leg` | `DEFAULT_OFFICIAL_LEG` 常量 | akshare |
| `leg_target` | AST 解析 `OFFICIAL_LEGS["akshare"].target` | `opendata_http.stock_feature.stock_hist_em:stock_zh_a_hist` |
| `leg_is_ported_akshare` | 上面那个 target 指向模块的前 6 行 | yes（`# Ported from akshare`） |
| `run_official_leg` / `run_official_target` | 留档 run 头部的 `official:` 行 | akshare / 与现值逐字相等 |

最后一条是**新鲜度钉**：仪器改了、run 没重跑，`run_official_target != leg_target` 直接判 stale。
留档命令刻意**不写** `--official`，所以报告头那一行同时是「默认值」和「实际执行的那条腿」的证据。

没有做的钉：给 `OFFICIAL_LEGS` 算摘要。摘要规则要在仪器和探针两边各实现一遍，两边一漂就变成假 stale；
现在的钉能吃掉「换腿」和「run 过期」，吃不掉「改分派表里跟腿无关的字段」，这一点写在这里而不是假装覆盖。

## 2. 真机 run：PASS 0 / FAIL 1 / ERROR 9〔档案〕

`qfq-official-akshare.txt`（`QFQ_EXIT=1`，窗口 2026-01-05 .. 2026-09-23，钉在 `dwd_stock_adjust` 的天花板）：

```
| 600519 | 131 | 29 | 0.996947 | 5.19e-03 | 3.15e-03 | 0 | FAIL |
```

- 131 根里 **29 根超容差**，跨 2026-02-05..2026-07-21；level 平台分布 `1.00306x19 0.99987x4 0.99904x3 …`。
  这两列是本轮新加的：`MAX_DIFFS=5` 只截列表，不截真相 —— 旧读法会把「29 根」看成「5 根」。
- 其余 9 个 symbol/method 对是 **ERROR**（em 端点 502 或空帧，腿按 3 次退避重试后仍拿不到）。
  空帧一律算 ERROR，不当对照 —— 否则「官方没答话」会被读成「官方没意见」。

## 3. 归因：那 29 根不在数据里，在两条链的算法里〔档案 `qfq-attribution.txt`〕

同窗口、同仓库、同一条腿，四段读数：

- **A 段**：我们的因子链 177 行里只有 **2 个取值**，窗口内只在 **2026-06-26** 换过一次值
  （`0.976879605643 → 1.000000000000`）。⇒ 超容差的 bar **不是除息日 bar**，「因子表少了一天」这类解释被排除。
- **B 段**：把 `ours/official` 除掉中位数 anchor 后按连续段展开，是一条从 `0.99785`（2026-02-05）
  单调爬到 `1.00315`（2026-06-24）、然后在 **2026-06-25..07-21 的 19 根上锁死在 1.00306** 的斜坡。
  ⇒ 不是「个别怪 bar」，是随日期/价格系统性漂移；超容差连续段 11 段 / 29 根，与第 2 节的 29 根**互相对上**。
- **C 段**：sina 不复权 177 根，与我们 `dwd_stock_daily` 的 131 根共同 bar **差异 0 根**，最大相对差 `0.00e+00`。
  ⇒ 原始 bar 层没有分歧。（同一段里 em 的不复权取数 3 次全空帧，档案里就写 ERROR，不拿 sina 顶 em 的位。）
- **D 段（机制检验）**：用 C 段已核对过的不复权价对官方 qfq 做拟合，
  `official/raw = 1.000000000 − 28.02 × (1/raw)`，截距偏离 1 = `-1.11e-16`，
  事件前其余 **111 根残差 max 0.0000 元**；事件之后 **18 根** `official` 与不复权价最大绝对差 **0.0000 元**。
  ⇒ **〔推断，由 D 段证伪式检验支撑〕** 官方那条是「价格 − 每股现金分红 28.02 元」的**减法链**，
  我们是「不复权价 × 乘性因子」的**乘法链**。两者只在最后一个事件之后逐根相等；事件之前的相对差是
  `D/P(t)`，随价格水平漂移 —— 这正是 B 段那条斜坡，也是 19 根尾段 level 恰好等于 `1/anchor` 的原因。

**〔推断〕的边界**：D 段能证明「官方链在本窗口内是逐根 `raw − 28.02`」，不能证明 em 内部实现就是这一行公式，
也不能证明我们那颗 `0.976879605643` 是从哪个参考价算出来的。把两条链在同一个价格上摆平只有一种可能：
`P × (1 − 0.976879605643) = 28.02` 给出 `P = 1211.9` —— 也就是说我们的乘性因子等价于「在 1211.9 这个参考价上扣 28.02」，
而 6 月的实际不复权价在 1268（档案里 2026-06-04 官方 1239.98 + 28.02 = 1268.00），在那个价位上乘法链扣的是
`1268 × 0.02312 = 29.32` 元，比官方的 28.02 多 4.6%。这两句都是对档案数字做的代数，不是量到的事实；
要把它做成判据面得补一次事件级对账（分红金额、除息日、参考价逐笔对上），本轮只登记，不假装已经量到。

## 4. 为什么不放宽容差

判据写的是「一致（容忍度内）」，不是「量级一致」。第 3 节的形状差是**口径差**：乘法链和减法链在价格离事件越远时
相对差越大，放宽容差只会把「两条链对分红怎么处理不一致」这个真实缺陷读成通过。所以：容差、逐日 shape dev、
ERROR 不算对照这三条，一条没动；改的是**测量**（over_tolerance / deviating_span / level 分布 / 连续段），
不是**判据**。

## 5. 反事实：12 条 break，和一条被 `--self-test` 抓到的 unreachable judge

探针 `AC-11|02` 现在有 12 条 break（默认腿回 sina、target 指向 sina 模块、模块不再声明 akshare 出身、
run 走的腿与当场默认腿不一致、run 过期、9 行 ERROR、有 FAIL、空跑、节点面少一条……）。
本轮第一次跑 `--self-test` 报的不是「哪条 break 没咬住」，而是：

```
AC-11|02: no reading this probe declares can make the judge pass, so a gap here could never be closed by real work
```

原因是我写的补齐读数把 `run_ok_rows` 抄成 `*run_ok_rows`，而当场量到的就是 0 —— 判据在任何真实工作之后都到不了
proven。改成声明「5 标的 × 2 方法 = 10 行全部对照、0 FAIL、0 ERROR」后：`resolve_repair → proven`、
12/12 break → gap（见 `probe-self-test.txt`）。这条 reachable 钉是本轮唯一真正拦住我的机器判据，
它拦的是**探针自己**，不是产品代码。

面基线：`--sync-faces` 后 `docs/quality/acceptance-probe-faces.json` **无 diff**（41 个探针 / 12 个在读 moment 面 /
proven 而无探针 7 个），本轮没有把任何格子从「读文件」变成「读时刻」。

## 6. 补齐路径（这是一条开发项，不是等证据）

1. 让服务端合成能表达减法口径：`dwd_stock_adjust` 现在只承载一颗乘性 `qfq_factor`，
   要按事件做 `raw − 现金分红`（含送转/配股的混合事件），就得让因子表或事件表能装下每股金额与比例；
   或者反过来，把 `qfq_factor` 的算法换成与官方同口径。
2. 换完重跑一次真机对照，要求 ERROR 行为 0（em 端点抖动已由重试面显式化，不能靠多跑几次蒙过去）。
3. 探针的判据一行都不用改：`run_fail_rows=0 ∧ run_error_rows=0 ∧ run_ok_rows>0` 就是为这条路径准备的。

## 7. 本轮没做的事

- 没动 `opendata/`（服务端合成逻辑一行未改）：本轮改的是仪器、探针、测试与文档，产品事实保持原样。
- 没重跑别的轮的档案，也没改它们；`docs/evidence/B4/qfq-official-check.txt` 保持原样，
  台账里仍指向它作为「C55 之前的读法」的对照物。
- 其余四个符号的官方对照没拿到（ERROR），本轮不去凑一个「重试到成功」的 run；
  档案里 retries 列写的就是第几次才拿到 / 或者一直没拿到。
- AC-8 的八格条目级面（任务 #69）没开始 —— 档案目录号已占用 C56，AC-8 那轮从 C57 起。

## 8. 档案表

| 文件 | 内容 | 生成命令 |
|---|---|---|
| `qfq-official-akshare.txt` | 真机对照 run 全文（腿名 + target + 每行 8 列 + `QFQ_EXIT=1`） | `bash docs/evidence/C56/run_qfq_official_akshare.sh` |
| `qfq-official-akshare-report.txt` | 仪器自己写的报告正文（同上未裁剪部分） | 同上（`--out`） |
| `qfq-attribution.txt` | 归因四段 A/B/C/D | `bash docs/evidence/C56/run_qfq_attribution.sh` |
| `probe-ac11-02.txt` | 单格复算读数 | `python scripts/quality/acceptance_item_probe.py --item 'AC-11\|02'` |
| `probe-self-test.txt` | 反事实全量复算（含 unreachable judge 的修法） | `bash docs/evidence/C56/run_probe_recompute.sh` |
| `probe-all.txt` | 41 格与台账逐格对账 | 同上 |
| `probe-gate-check.txt` | 门禁成员自检面 | 同上 |
| `run_*.sh` | 三个留档包装脚本（含 provenance 头） | — |
