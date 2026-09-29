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
- 时间：`--self-test` 一遍约 27 分钟（五格单项跑 6.4 s）。本轮内新测初版把反事实遍拖到被外部超时打死，
  且命令尾接 `tail` 使 `$?` 取的是管道尾退出码、把失败掩成 exit 0；重跑改成不接管道并把
  `SELF_TEST_EXIT=$?` 写进档案，才拿到真实的 FAIL 读数（即上一节那条）。
- `AC-16|01`/`|02` 的 gap 是仓库里**缺那份逐包人工留档**，不是量不到；历史提交说明补不回来
  （不改写历史），本轮不代拟审查记录。`|02` 的「近似复制」只有真做逐包比对才能翻正。
- `|01/|02/|03/|04` 依赖 git 历史与跟踪清单 ⇒ 属 moment 面，终值以干净树的 `make gate` 遍为准；
  档案里的 `(document line …)` 是回填前行号。
- `AC-16|06`（基线清零）仍是 task #64 那笔用户/产品决策，本轮不重复改判；`|05`/`|07`/`|09` 的
  原判定面与读数一字未动。
