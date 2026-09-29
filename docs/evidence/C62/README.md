# C62 — AC-16 许可与来源边界五格做成条目级判定面（|01/|02/|03/|04/|08）

- 轮次：C62（任务 #76）；日期 2026-09-29；基线提交 `d0e2c51`（branch `dev`）。
- 改动面：`scripts/quality/acceptance_item_probe.py`（五格 `measure/judge/breaks/repair` + 有界缓存 +
  一处 judge 强化）、`THIRD_PARTY_NOTICES.md`（一条假指针更正为实际路径）、验收文档 v5.35、
  `docs/quality/acceptance-item-ledger.json`、`docs/quality/acceptance-probe-faces.json`。
- 判据原文一字未动；§2 只做加性注记与翻勾选符号；本轮没有放宽任何既有判定条款。

## 结论（五格 census：gap=2，proven=3）

| 格 | 状态 | 一句话理由 |
| --- | --- | --- |
| AC-16\|01 | gap | 逐包审查留档从没成文：7 个自研 provider 包的提交声明核查 0/7、留档核查 0/7 |
| AC-16\|02 | gap | 否定面量全（517/517 解析、88 个 import 根、0 个 openbb），但判据括号的两项人工核查 0/7 |
| AC-16\|03 | proven | 搬运树 313 文件 MIT/来源逐读 0 缺，来源 commit 单值且等于 lock；台账假指针本轮已更正 |
| AC-16\|04 | proven | 静态三面 0 命中 + BSL 三包拒于 `sys.meta_path` 后 3 个 MIT 入口 `IMPORTED=3`/exit 0 |
| AC-16\|08 | proven | LICENSE 首行逐字 BSL 1.1、四必填条款 4/4、README 与数据权利登记各有免责正文 |

## 读数出处（行号即档案行号，未裁剪）

`probe-items-ac16.txt`（五格一次跑，`--item` 可重复；跑于文档回填**之前**，故档案内
`(document line 268/269/270/271/275)` 是回填前的行号，回填后各 +1 行，见下表）：

- 第 6 行：自研 provider 包 7 个（git 跟踪清单里带 `registration.py` 的目录）；
  第 8/9 行：提交声明 0 个、逐包留档覆盖 0 个；第 10 行：5 个包的 docstring 自声明（不认）。
- 第 18 行：运行时四包 517 个跟踪 py、AST 解析成功 517 个、88 个不同顶层 import 根、0 个 openbb 根；
  第 19 行：`FORBIDDEN_ROOTS =（akshare, openbb）`、扫描器正文 openbb 出现 5 处（含故意违规样本）；
  第 20 行：字面出现 openbb 的运行时文件 6 个（对照表加载器 + provider docstring）；
  第 21 行：两项核查 0/7 与 0/7。
- 第 29/30/31/32 行：313 文件逐读缺 MIT 0 / 缺来源 0、commit 单值等于 `c4f6a631c259`、
  台账三读 yes、台账点名路径磁盘/跟踪不存在的 0 个。
- 第 39/40 行：搬运树 `registration.py` 0 个 / `*/providers/*` 0 个 / import BSL 根 0 个；
  meta_path 闸门自证会响 + 3 入口 `IMPORTED=3`、exit 0、失败读数 `(absent)`。
- 第 47/48 行：LICENSE 首行逐字 + BSL 四必填 4/4；README 与 `docs/data-rights-registry.md` 免责正文均在位。
- 第 51 行：`census: gap=2, proven=3`。

`self-test-run1-failed.txt`（第一遍反事实，`SELF_TEST_EXIT=1` 在第 4 行）：
第 2 行点名没能咬住的反事实与后果 ——「AC-16|02 / 一个运行时文件解析不了，走查面比跟踪面小:
the judge still says proven」。
`self-test.txt`（修后重跑，`SELF_TEST_EXIT=0` 在第 3 行）：第 2 行
`OK: 59 probe(s) measured … all 458 counterfact(s) flip a clean reading back to a gap`。

`guards.txt`：第 8 行 `collected 42 items`，第 14 行 `42 passed in 68.66s`；
命令为 `OPENDATA_DB_HOST=127.0.0.1 OPENDATA_DB_PORT=1 python -m pytest
tests/test_acceptance_probe_gate.py tests/test_p0_integration_surface.py -m "not e2e" --no-cov -q`
（仓库指向 127.0.0.1:1 端口 ⇒ 任何真库连接必然连不上，单元面不碰生产仓库；e2e 全部 deselected）。

## 本轮的判据强化（不是放宽）

第一遍 `--self-test` 是红的：给 `AC-16|02` 施加「走查面比跟踪面小（516/517）」时判定仍读 proven，
因为 `judge_ac16_02` 原来只要求解析数 `> 0` —— 被静默跳过的运行时文件不会让这条变红，
于是「0 个 openbb import 根」可能建立在残缺覆盖上。修法是把「解析数 == 跟踪文件数」钉进判定式顶层，
并给 gap 理由加一支「走查面 ≠ 跟踪面」的归因（原判据原文、原六条合取一字未减）。
修后：反事实遍全绿、五格读数不变（`gap=2, proven=3`）、`--sync-faces` 写回后面基线与写前
逐字节相同（`FACES_IDENTICAL=yes`，新判定不引入新的 git 时刻面）。

## 代价与已知边界

- 仪器面：探针 54 → 59，反事实 430 → 458，在读 moment 面 13 → 17，`proven 而无探针` 仍 7 个。
- 时间：`--self-test` 一遍约 27 分钟（五格一次跑实测 6.77 s user 2.73 s；回填后再跑一次，
  五格的「台账现状」行读成 `state=gap / gap / proven / proven / proven`，与本轮写入台账的
  状态逐格一致 —— 文档、台账、探针三面在这一点上闭合）。本轮内新测初版把反事实遍拖到被外部超时打死，
  且命令尾接 `tail` 使 `$?` 取的是管道尾退出码、把失败掩成 exit 0；重跑改成不接管道并把
  `SELF_TEST_EXIT=$?` 写进档案，才拿到真实的 FAIL 读数（即上一节那条）。
- `AC-16|01`/`|02` 的 gap 是仓库里**缺那份逐包人工留档**，不是量不到；历史提交说明补不回来
  （不改写历史），本轮不代拟审查记录。`|02` 的「近似复制」只有真做逐包比对才能翻正。
- `|01/|02/|03/|04` 依赖 git 历史与跟踪清单 ⇒ 属 moment 面，终值以干净树的 `make gate` 遍为准；
  档案里的 `(document line …)` 是回填前行号。
- `AC-16|06`（基线清零）仍是 task #64 那笔用户/产品决策，本轮不重复改判；`|05`/`|07`/`|09` 的
  原判定面与读数一字未动。

## 门禁遍露出的第二处缺陷：探针把自己的档案读成了审查留档（同一轮自查）

干净树的 `make gate` 遍（档案 `gate-run1.txt` 第 515/516 行）把 `|01`/`|02` 的留档面读成 **7/7**，而本轮逐项跑读的是 0/7。差异不是时刻面漂移，是**仪器被自己喂饱**：`record_bodies` 原来按整文件匹配（正文同时出现「无 OpenBB 源码参照」与「审查」即算候选），`clean_room_records` 再按包名做全文匹配 —— 而本轮写进 `docs/evidence/C62/` 的档案正好同时满足这两条（它点名 7 个包并引用那句声明）。也就是说：一次「留档从没做过」的判据，被本轮自己写下的测量记录翻成了 7/7。

修法（只加严、不放宽）：
1. `PROBE_ECHO_MARKERS`：带探针读数同形标记（`VERDICT `／`判据原文：`／`本探针：`／`台账现状：`）的证据文件先剔除 —— 机读回声不是人工审查记录；
2. 绑定改成**行级**：同一行里既点到包名又写得到该句声明才算一条记录（文档可以在别处点名七个包、在另一处引用那句话，那是一次性引用）；
3. 新面 `recorded_binds`（绑定行数）进两格判定式顶层，各配一条反事实（`recorded_pkgs=7` 而 `recorded_binds=0` ⇒ gap），所以「计数没有行支撑」这种形状从此会咬。

修后复测：留档面回到 **0 包 / 0 绑定行**，五格 census 仍是 `gap=2, proven=3`（判定结论一字未变，变的只是那条被污染的读数）。守卫 `tests/test_acceptance_probe_gate.py` 38 passed；反事实总数 458 → 460（两格各多一条）。本轮的门禁遍档案不重写：`gate-run1.txt` 里 7/7 那两行按原样留档，就是这一节的证据。

## 修后复验：两次干净树门禁遍的对照读数（预测先登记，读数后落地）

修完后重跑两遍（`/tmp/c62/verify-chain.sh`：先 `make acceptance-probe-check` 单跑，绿了再 `make gate`），
两份日志**整份入档不裁剪**：`probe-check-after-fix.txt`（70 行，`PROBE_CHECK_EXIT=0` 在第 70 行）、
`gate-run2-final.txt`（7814 行，`===== gate: PASSED =====` 在 7813、`GATE_EXIT=0` 在 7814）。
下面每条都按 `grep -n` 的行号引，不复述记忆。

| 读数 | 修前那一遍（`gate-run1.txt`） | 修后这一遍 |
|------|------------------------------|------------|
| AC-16\|01 留档面 | 「留档覆盖 **7** 个」（第 515 行） | 「留档覆盖 **0** 个（同一行绑定 **0** 行；带探针读数同形的档案已先剔除）」（`gate-run2-final.txt` 第 515 行） |
| AC-16\|02 留档面 | 「人工抽查留档 **7/7**」（第 516 行） | 「**0/7**」（`gate-run2-final.txt` 第 516 行） |
| 反事实遍 | 458 条（修前 `self-test.txt`） | 「59/59 个探针走到了判定，**460** 条 break 各被施加一次、每条都要求把干净读数打回 gap」（`probe-check-after-fix.txt` 第 65 行） |
| 台账↔读数 | —— | `agrees=59, unflipped=0, open=0, deferred=0, stale-proof=0`（同档第 66 行） |
| 面基线 | 13 个 moment 面 | 「59 个探针里 **17** 个在读 moment 面；proven 而无探针 7 个，基线 7 个（只降不升）」（同档第 67 行） |
| 成员自证 | —— | `acceptance-probe-check` 在 `gate:` 配方里 = yes（同档第 68 行） |
| 五格终值 | gap=2 / proven=3 | **同形不翻**：`gate-run2-final.txt` 第 515/516 行 gap，第 517/518/521 行 proven |
| 门禁台账普查 | —— | `items=130 proven=50 gap=16 unreviewed=64 ticked=50`（`gate-run2-final.txt` 第 46/47 行） |

本轮把预测写在跑之前（`docs/evidence/C62/predictions-pre-registered.txt`，写下时间 2026-09-29T02:01Z，早于两份日志生成）：六条预测里
①460 条、②留档 0 包/0 绑定行、③`GATE_EXIT=0` + 7800 行上下、④五格不翻、⑤普查行不变
——**五条逐字命中**；第⑥条（若修后仍读到非零留档面则说明还有第二处回声源）未被触发。
单跑 `--gate-check` 的墙钟 895.2 s（`probe-check-after-fix.txt` 第 4 行），比门禁遍里那一位（修前
1299.6 s，`gate-run1.txt` 第 507 行）快，是因为这一遍没有并行的其余 16 个成员抢 CPU；工作树在开跑前是干净的（同档第 5 行）。

**如实登记一个档案顺序缺陷**：`gate-run1.txt` 是本轮第一次干净树门禁遍，它当时只落在 `/tmp`，
而上一节的披露段已经按行号引了它 —— 引证先于档案入仓，构成一段悬空引证。本节把它补进本目录
（7812 行整份，未裁），并把 515/516 两行的原文对齐到表格里；7/7 那两行**按原样保留不重写**。
