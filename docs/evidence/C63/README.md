# C63 — AC-19 运维保障五格做成条目级判定面（|01/|02/|03/|04/|05）

- 轮次：C63（任务 #75）；日期 2026-09-29；基线提交 `c8a3fe7`（branch `dev`）。
- 改动面：`scripts/quality/acceptance_item_probe.py`（五格 `measure/judge/breaks/repair` + 十个新
  helper：`code_lines` / `mysqldump_invocations` / `ac19_tracked` / `schedule_sites_for` /
  `drill_checklist` / `status_class_rules` / `filled_cells` / `config_doc_rows` / `retention_callers` /
  `key_expiry_signal`，另加两条语料常量 `BINLOG_VALUE`（只认 ON/OFF 的实取形状）与
  `ATTESTATION_SKIP_PREFIXES`（「实测取值」语料的剔除面））、
  验收文档 v5.36、`docs/quality/acceptance-item-ledger.json`（五格 re-key + 读数）、
  `docs/quality/acceptance-probe-faces.json`（`--sync-faces`，纯增量）。
- 判据原文一字未动；§2 只做加性注记 + 两个勾选符号（`|02`、`|05`）；本轮没有放宽任何既有判定条款，
  也没有新增 `subprocess` 调用点（bandit selfdev 棘轮冻结在 1，历史闸走既有的 `run_argv`）。

## 结论（五格 census：gap=3，proven=2）

| 格 | 状态 | 一句话理由 |
| --- | --- | --- |
| AC-19\|01 | gap | 脚本/手册/一次真演练都在位，唯独仓库内**每日触发点 0 处**；binlog 只剩标为「可选」的附录，且 0 份档案带一次实测取值（语料已剔掉整个档案面与结论面） |
| AC-19\|02 | proven | 档案早于本轮（2026-09-22 首次入库）+ 表头之下 6 步逐条 ✅ + 两个隔离恢复库 + 行数比对 + `/health` healthy + A0 DoD 点名 |
| AC-19\|03 | gap | 策略 4/4、执行器 4/4、配置键 4/4 全在位，运行时四包**生产调用点 0 处** ⇒ 一套只有测试会跑的执行器 |
| AC-19\|04 | gap | 类别 4/4 与 401/403、429 两条线上归类在位，但「到期」无可触发信号、带外告警通道 0 处 ⇒ 只有 `/health` 可拉取 |
| AC-19\|05 | proven | 在册清单 54/54 行四列齐且无空单元格；「生效方式」以第 5 行全局条款承载，且与 `@lru_cache` 的代码事实自洽（自洽闸另判） |

仪器总量（本轮前后）：探针 59 → **64**，反事实 460 → **488**（AC-19 五格 6/7/4/7/4 = 28 条），
moment 面探针 17 → **22**，proven 而无判定面 7 → 7（不变）。

## 读数出处（行号即档案行号，未裁剪）

`probe-items-ac19.txt`（五格一次跑，跑于 v5.36 文档回填与台账 re-key **之后**：档案里的
`(document line 257…261)` 就是验收文档此刻的行号，`台账现状` 行读的也是回填后的键；回填只在同一行尾部
加注记、不插行，所以下面引用的行号与本档案一致；文件末尾第 55 行是 `ITEMS_EXIT=0`。这一遍刻意跑在
回声修复之后：语料此刻已把整个档案面与结论面剔掉，所以这份档案此后再怎么改写、再入库，都不会反过来
移动它自己第 9 行那个 0。扫描人口 1625 是 `6a8ea23` 之后的口径，下面的门禁遍把 `gate.txt` 补进档案面会让它
变 1626 —— 各格读数按跑动时刻冻结，不做回填）：

- 第 6 行：备份脚本在跟踪清单 = yes，`dump_one` 调用 2 次（metadata、warehouse），`mysqldump` 真实调用
  1 处（前置检查那行不计）；第 7 行：**扫描人口 1625 个跟踪文件**，每日触发点 0 处；
  第 8 行：RPO ≤24h 声明 yes、binlog 写进手册 yes 且手册把它列为可选附录；
  第 9 行：实测过的 `log_bin` 取值 **0 份**档案，并把剔除面逐字读出来（整个 `docs/evidence/` 档案面、
  `docs/quality/`、`docs/迭代计划/`）；第 10 行：`repair` 面（缺什么，落点是手册而不是任何档案面）；第 11 行 verdict。
- 第 17 行：演练档案在跟踪清单 + 检查单表头读得到（同档另有两张也从 `| 1 |` 起头的表，判定只读表头之下的行）；
  第 18 行：首次入库 2026-09-22（提交 `0a9fba28df98`）、早于本轮 = yes；第 19 行：6 步 / 6 个 ✅；
  第 20 行：两个隔离恢复库 2/2 + 行数比对 yes + `status=healthy` yes；第 21 行：A0 DoD 点名 yes；第 22 行 proven。
- 第 28 行：策略 4/4 + 执行器 4/4（四个具名函数）+ 配置键 4/4；第 29 行：生产侧调用点 0 处；第 31 行 verdict。
- 第 37 行：四类健康类别 4/4；第 38 行：401/403→credential-rejected yes、429→quota-exhausted yes、
  到期分类或主动探测 **no**（本平台自签 Key 的 `is_expired` 不计，判据点名权威源）；
  第 39 行：分级落点 1 处（`opendata/api/pipeline.py`，定义处 `patrol.py` 已排除）+ 带外通道 0 处；
  第 40 行：模块自己的边界披露句在位；第 42 行 verdict。
- 第 48 行：在用键名行 54 条 / 四列齐 54 条（另有 1 条属第十节两列表，不计入）；第 49 行：逐行「生效方式」
  表头 0 处、全局条款 yes、逐行「热加载」标注 0 条；第 50 行：4 个运行时模块在调用点读 `get_settings()`
  且 `get_settings` 仍被 `@lru_cache` 冻结 = yes；第 51 行 proven。第 53 行：`census: gap=3, proven=2`。

`self-test.txt`（反事实遍，三行不裁）：**跑了三遍**，`013f9c9` 之后一遍、`6a8ea23`（语料回声第一步）之后
一遍、剔除面固化为整个档案面之后再复验一遍，三遍**逐字节相同**（第三遍即当前档案）。第 1 行 `64/64 个探针走到了判定，488 条 break 各被施加一次、
每条都要求把干净读数打回 gap`；第 2 行 `OK: …all 488 counterfact(s) flip a clean reading back to a gap.`；
第 3 行 `SELF_TEST_EXIT=0`。**没有一条新反事实咬不动**：回声修复动的是语料剔除面，|01 那 6 条 break 一字未动。
中间另跑了一遍单成员 `--gate-check`（档案 `probe-check-final.txt`，75 行整份不裁）：第 3 行墙钟 1085.1 s、
64/64 探针读到事实；第 69 行反事实面 `64/64 个探针走到了判定，488 条 break 各被施加一次`；第 70 行
`agrees=63, unflipped=0, open=0, deferred=1, stale-proof=0`；第 71 行 deferred 那格是 `AC-1|10` 的时刻面 ——
开跑前工作树有 6 个改动项（第 4 行逐字点名 `M docs/evidence/C63/README.md`），时刻面按既有约定只记 deferred
不记漂，终值以下面的干净树 `make gate` 遍为准；第 72 行面基线 `64 个探针里 22 个在读 moment 面；proven 而无探针 7 个，基线 7 个`；
第 74 行 `OK: …五面全过`、第 75 行 `PROBE_CHECK_EXIT=0`。同一遍之后另跑了 `--sync-faces`，写回后 `git status`
对该文件**无差异** ⇒ 本轮没有让任何探针悄悄丢掉或新增 moment 面。AC-19 五格在该档第 27～31 行，
与本档案 `probe-items-ac19.txt` 同形（gap / proven / gap / gap / proven）。

`guards.txt`（守卫用例，不测具体读数而测仪器自身的形状约束；命令
`python -m pytest tests/test_acceptance_probe_gate.py -m "not e2e" --no-cov -q`）：第 8 行
`collected 38 items`、第 13 行 `38 passed in 51.56s`、第 14 行 `GUARDS_EXIT=0`（这一遍跑在剔除面固化之后，
是回声修复前的 67.55s 那一遍的复验）。其中与本轮直接相关的两条：
`test_the_committed_baseline_agrees_with_the_source_right_now`（面基线要与源码现算逐探针一致 —— 本轮
`--sync-faces` 的 27 行增量因此不是手写的）与 `test_each_new_probe_breaks_a_face_this_round_actually_paid_for`
（本轮新面配的每条 break 都要真咬；五格 6/7/4/7/4 = 28 条，见上）。

## 本轮先修掉的四处仪器自身缺陷（都不在判据里，都在读数里）

1. **`git ls-files` 的八进制转义让在册文档读成「不存在」**（AC-19\|05、\|01 都受影响）。`core.quotePath`
   默认为真 ⇒ 非 ASCII 路径以 `"docs/\351\205\215…"` 形式输出，任何 `字面名 in ctx.tracked()` 的判定
   对 `docs/配置项清单.md` 永假。这不是判据问题，是仪器问题：如果照原样跑，一条在册清单会被记成
   `doc_tracked=no` + 0 行读数，而 0 读数看起来像「文档很薄」。**正向对照先行**：改读 `git ls-files -z`
   后同一调用返回 1621 条、其中该文件名可逐字指认（本轮档案入库后复算为 1625 条，两个人口下都可指认），
   才把 `ac19_tracked()` 换进去（全仓库非 ASCII 的跟踪路径今日只落在 `docs/` 下，但这条盲区对所有按
   `ctx.tracked()` 做字面成员判定的探针都成立，已作为后续项登记，不在本轮动别的探针）。
2. **探针自己的字符串常量冒充仓库里的调度定义**（AC-19\|01，即 C62 机读回声的新 costumes）。
   `schedule_sites_for` 扫非注释行里的 `backup_mysql`，而 `acceptance_item_probe.py` 自己那行
   `AC19_BACKUP_REL = "scripts/ops/backup_mysql.sh"` 正好是含 needle 的代码行 ⇒ 探针会把自己的常量表
   读成「有一处每日触发点」，把真 gap 洗成 proven。现按 `PROBE_SELF_REL` + 被调脚本自身一并排除，
   并把**扫描人口**打进读数（第 7 行），使「0 处」说得清是在 1625 个文件里读到 0。
3. **按字面抓行会把别表的行当检查单**（AC-19\|02）与**把前置检查那行 `mysqldump` 当 dump 调用**
   （AC-19\|01）。前者改为只在 `| # | 步骤 | 通过判据 | 结果 |` 表头之下取行 —— 同一份
   `restore-drill.txt` 里另有两张也从 `| 1 |` 起头的表（§4 缺陷表 8 行、§5 移交 A4 表 3 行，档案第
   92/105 行各有自己的表头），按前导数字抓行会把它们混进步数；后者改为按语句行首
   `^(?:if ! )?mysqldump\b` 计数，于是「两库各一次全量」是 `dump_one` 2 次 + 真实 `mysqldump` 1 处
   （脚本用循环共用一次调用），两个面各判各的。
4. **「实测取值」那一格把本轮自己的陈述当成了读数**（AC-19\|01，同一提交后复算才露出来）。第一次回填跑
   `--item 'AC-19|01'` 时 `binlog_attested=0`；把本轮档案与 v5.36 回填提交上去再跑，同一格读成 **4 份**
   —— 命中面是 `docs/evidence/C63/README.md`、`docs/evidence/C63/probe-items-ac19.txt`、
   `docs/quality/acceptance-item-ledger.json`、`docs/迭代计划/…/验收文档.md`，全是本轮自己写的「0 份档案带
   实测取值」那句话，没有一份是实测。两处根因：语料只按 `docs/` 前缀筛（结论面与本轮档案目录都落在里面），
   而匹配式 `log_bin\s*[:=]\s*(?:ON|OFF)` 会把文档里作为**格式串**出现的 `` `log_bin = ON|OFF` `` 读成一个取值。
   修分两步。**第一步**（`6a8ea23`）只认 ON/OFF 的实取形状（`` `(?!\|)` `` 把格式串排除）并剔掉本轮自己的
   目录 + 结论面。**第二步**把「本轮自己的目录」这种**按最大 N 动态取**的写法换成了静态的整个档案面
   `ATTESTATION_SKIP_PREFIXES = ("docs/evidence/", "docs/quality/", "docs/迭代计划/")`，理由是一个时间炸弹：
   动态写法到下一轮剔的就是下一轮的目录，而 C63 这一轮为说明这条修复而在档案里写下的取值形状（本文上一段
   就有三例），会从这里开始被 C64 及之后**每一轮**读成一次实测 —— 每个后续的 0 都不是同一个 0，且会随仓库里
   存留的历史陈述漂移。剔除面本身打进读数（档案第 9 行逐字可读），所以口径是谁剔掉的、剔了哪几面前置可见。
   修后复算回到 **0 份**，且这 0 不再随本轮档案增减而漂移（本轮档案与台账此刻都在被剔面上）。判据结论没动
   （\|01 仍因每日触发点 0 处判 gap），但修前那个「4」是仪器自造的假阳性 —— 若当日每日触发点那一面先被补上，
   这格就会把 gap 洗成 proven。
   写档案时另撞到同形回声一次：为说明这条修复而在 §10 写的 `` `log_bin = ON/OFF` ``，在收紧匹配式之后
   仍被读成一次实取（新式下 4 份命中只剩它这一份，而它照样不是实测）。所以这一格的口径是**剔除面**决定的，
   不是措辞小心决定的 —— 该句已改写，剔除面则在读数里逐字可见（档案第 9 行）。
   匹配器本身另跑了正向对照（临时 python，未落文件，可一行复现）：`log_bin = ON`、`log_bin=OFF`、
   `log_bin : Off` 三种形状全部命中，`` `log_bin = ON|OFF` ``（格式串）、`log_bin = /var/lib/mysql/mysql-bin`
   （配置片段）与「log_bin 已开启」三类全部不命中 ⇒ 那个 0 是活匹配器读出的 0，不是一条永不成立的式子。

## 代价与已知边界

- 三格 gap 的收口动作都在仓库外或需授权，本轮**不代拟**：\|01 要把备份接进调度（部署动作）并做一次
  真 `log_bin` 取值（连库确认；`repair` 给的落点是 `docs/operations-backup-restore.md`，落进 `docs/evidence/`
  的任何一档、或台账与验收文档的结论面，都会被同一道剔除面剔掉，见上第 4 处缺陷）；\|03 一旦接线就会开始**真删**生产数据（分钟线按 N 年、差异明细按天），
  属破坏性变更，与 task #52/#64 同列等用户确认；\|04 需要一条带外通知通道与运维台账侧的 Key 颁发日记录。
  各格的 `repair` 面在档案第 10/30/41 行逐字可读，写的是「缺哪个面、补到什么值」。
- \|05 的 proven 是按判据字面（文档存在 + 键名/含义/改动影响/生效方式四字段有承载）给的，
  「生效方式」目前是**一句全局条款**而不是逐行列：文档里 0 处带该列的表头、0 条逐行「热加载」标注。
  本轮把它钉成一条自洽闸而不是加分：代码侧若去掉 `@lru_cache`（配置真能不重启即生效）而逐行仍无标注，
  该格即判 gap —— 全局条款就成了假话。补「逐行生效方式列」属文档加宽，登记不代拟。
- \|02 的历史闸（档案首次入库必须早于本轮）用 `git log --diff-filter=A` 读，走既有 `run_argv`，
  因此本格读数带 `history(git log)` moment 面：改写历史或在新克隆里跑都会改变它，这是刻意的。
