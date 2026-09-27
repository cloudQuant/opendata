# C49 — AC-9\|03/08/09「反例用例 + 单源域直通可用 + 重算幂等」：两格的判定面先前根本不咬，一格的洞在代码里

本轮把 AC-9 剩下三格「单元可判」的判据做成可证伪的条目级判定面，并修掉量出来的两个真洞：① 一侧宽松的 `tolerances` 会把另一侧紧的声明**整本**盖掉；② 一个两义的键让 `merge_source_frames` 整窗 `raise`，于是 `stock_action` 的 dwd 直通从来没交出过一行。条目级 `3/9 → 5/9`（\|03、\|09 挣到；\|08 代码面全通但表里 0 行，**不勾**）。

三格判据原文（回填前在 `验收文档.md` L175/L180/L181；本轮 v5.21 修订行插入后随之下移一位 ⇒ 现在是 L176/L181/L182）：

| 条目 | 判据原文 | 本轮结论 |
|------|------|------|
| AC-9\|03\|3bce2eae | **反例用例**：① 字段名不匹配时必须报错而非静默通过；② 单位未归一时必须产生 mismatch | proven（两条反例各有一个在生产入口上会响的判定，加 8 条反事实） |
| AC-9\|08\|a87bfe6f | **单源域 dwd 直通模式可用**（`layer=dwd` 查询不报错） | **gap，不勾**：整窗 `raise` 已换成逐键拒绝（55,073 → 55,071 行可直通），但 `dwd_stock_action` 实测 0 行，另外 3 个单源域连 dwd 表都没有 |
| AC-9\|09\|ca69bd21 | dwd 重算幂等 | proven（重算确定性 + 键级 upsert + 主键即业务键，三格节点 4/4 绿） |

## 〇、修前的真实形状（读自 `git show HEAD:…`，HEAD=`349f560`）

| 判据点名的面 | 修前已有的面 | 修前没有的面 |
|------|------|------|
| 「①字段名不匹配时必须**报错**」 | `compare_source_frames` 里确有 `missing mapped columns … (fail closed)` 的 `ValueError`，也有 `TestComparison::test_structural_field_mismatch_fails_closed` | 那条用例测的是**两侧契约模型不同名**（`different contract fields`），不是「mapping 声明了一列而帧里没有」。面 2 的 D 格实测才是后者：抛 `ValueError: source frame is missing mapped columns ['volume']`。当时**没有**任何用例钉住这一形状；面 3 又量出 `tests/test_cross_check.py` 全部既有用例的 marker 为空 ⇒ 门禁跑得到，但「跑得到」不等于「判据点名的两件事各有断言」 |
| 「②单位未归一时必须产生 mismatch」 | `test_unconverted_unit_produces_a_mismatch` 在位，断言 `verdict is DEVIATION` **且** `samples[0].field == "volume"`（点名字段，不是只读一个枚举） | 「归一之后到底可不可比」这一面当时没人钉。面 2 的 A/B/C 三格只覆盖空值形状（两侧同 `None`／同空串 = consistent，一侧 `None` 一侧有值 = deviation 判差 2、`per_field={'volume': 2}`）；G 格是「a 腿声明 `scale: 100`、b 腿原始值已是股」，读数 `consistent` —— 这个读数**是对的**（归一后逐值相等），但它对不对取决于两侧容差字典怎么配对，见下一行 |
| tolerance 从哪来（判据未点名，但它是 ②的判定分母） | 每份 yaml 自己声明 `tolerances` | 当时源码是 `tolerances = mapping_b.tolerances or mapping_a.tolerances` —— **整本字典**二选一。面 2 实测：`close 相差 0.4 @ a 紧(1e-4) / b 松(1.0) → verdict=consistent`，即一条腿的宽松声明能把另一条腿的紧声明**整段关掉**。今天两份 yaml 的 OHLC 容差逐字段相同（面 6 的实测声明面：`tolerance={'ths': 0.0001, 'akshare': 0.0001}`），所以这个洞在真机上还没有咬过人 —— 「还没咬人」不是「不存在」 |
| 「单源域 dwd 直通模式可用」 | `MergeStats.passthrough`（`len(frames) == 1`）、查询层不区分腿数（面 5：`query.py` 里出现过的映射/权威符号 = 无，`查询层是否会因腿数而改变行为：False`）、`dwd_stock_action` 表存在 | 两件事都没人做过：**① 直通到底交不交得出东西**。面 8 修前读数：`直通 merge 失败：raised ValueError: source 'ths' has duplicate business key ('603883', datetime.date(2024, 6, 27))` —— `_index` 对单个两义键**整窗 raise**，`DwdMergeService.run` 因此在 `stock_action` 上什么都不写；真库侧独立互证：`dwd_stock_action` **0 行**（面 5、面 8 两处），而它的 ods 腿有 55,073 行。**② `layer=dwd` 查询到底走不走得通**。面 7 用生产 `_inspect_columns`/`_key`/`build_data_select` 逐域实测：`dwd_futures_daily`/`dwd_index_daily`/`dwd_option_daily` 三张表**不存在** ⇒ `_inspect_columns` 吞掉异常返回 `[]`，endpoint 在建 SQL 之前就 404 |
| 「dwd 重算幂等」 | `all_keys = sorted(...)`、`DwdWriter.write` 走 `build_upsert_sql(table, columns, key)`、`_table_ddl` 的 `PRIMARY KEY` 就是业务键 | 三件事**在单元面上互不相干**：当时只有 `TestDwdWriteAgainstMysql` 那一条 `@pytest.mark.e2e` 断言幂等，而门禁选择式是 `-m "not e2e"` ⇒ 这一格唯一的真库见证**一次都没在门禁里跑过**（面 3 的 marker 列直接量出；探针读它的当前值是 `exit=5`，即被选择式全部 deselect）。结果是「幂等」这件事当时只以「看起来对了」的形式存在 |

四点结构性事实（不是措辞问题，是判定面问题）：

1. **「用例存在」与「判据点名的那件事有断言」是两件事**。②的断言点名了字段、也点名了 verdict，所以它是好的；缺的是①的第二种形状（mapping 声明而帧缺列）与配对面（一侧声明单位）。本轮没有把已有用例改窄或改宽，只是把缺的形状补上。
2. **`or` 配对的字典是一个「谁生效取决于谁在左边」的面**。它的危险不在算错，而在**算得出来且看起来算过**：紧声明那一侧完全不知道自己的容差被换掉了。修法是逐字段合并、取紧的那条（`_stricter_tolerances`），并把正反两个方向都量进用例。
3. **一个键两义 ≠ 整窗不可信**，但**也不是「留第一条」**。`dwd_stock_action` 按 `(symbol, ex_date)` 建主键，而 ths 腿把 603883 的 2024-06-27 除息日发了两次、两套方案（0.16 现金 vs 0.5 现金 + 0.3 送）—— 契约里没有任何一句话规定留哪条。整窗 `raise` 把「这一个键不可判」放大成「这一批都不能落」，且**放大的那一步是不可见的**（作业红一次就没人再看）。本轮改成：该键整体拒绝（两义组里一条都不留）、其余照常落、被拒绝的键逐条进 `stats.colliding` 报告。
4. **门禁跑不到的见证等于没有见证**。这一点本轮第三次在 AC-9 里量到（C48 是 kind 声明与执行器两个集合；本轮是 e2e 标记与门禁选择式）。因此 \|09 的判定面刻意建在**门禁跑得到**的三条代码面 + 5 条纯函数节点上，真库那一格只作为**披露**读数值出现（`exit=5`），不作为判据。

## 一、先量：八个面，跑在活库上（只读）

`ac9_residual_census.py`（`set -a; . ./.env; set +a; python docs/evidence/C49/ac9_residual_census.py`）：全程只读，脚本自带的非只读语句闸门两遍都是 0 条，不建表、不写入、不打印连接串（`warehouse engine alive (url never printed): probe=1`）。修前读数 `census-pre-fix.txt`（`CENSUS_EXIT=0`，档案 157 行 = 14 行抬头 + 143 行正文，抬头带 run_at / branch / HEAD / worktree / python / command）读出 **65 条语句 → `{'SELECT': 45, 'SHOW': 20}`**；修后权威读数 `census-post-fix.txt`（同一脚本同一命令，`exit=0`，档案 222 行 = 8 行抬头 + 214 行正文）读出 **66 条 → `{'SELECT': 46, 'SHOW': 20}`**。多出来的那一条 SELECT 不是新加了一次测量：面 8 在直通真的跑通之后才读得到 `dwd 表 dwd_stock_action 实际行数=0` 那一行，修前那一遍在整窗 `raise` 处就退出了。八面：

1. **判据与仪器面**：三格原文逐字打印（含 ledger key 的 sha8 与文档行号）+ 台账现状（当时三格都是 `unreviewed`）+ **有没有条目级探针**（修前：三格全为「无」）。
2. **反例面**：`compare_source_frames` 对七种输入（A–G）的实测反应，再加 tolerance 配对的四种组合。两个洞就在这一面：D 格证明「帧缺 mapping 列」是报错的但没人钉；`close 相差 0.4 @ a 紧 / b 松 → consistent` 是**修前**读出来的假绿，修后同一格是 `deviation 判差=1 per_field={'close': 1}`。
3. **见证用例面**：每个相关节点的 marker 与断言原文 —— 用来判「这条断言在门禁的选择式下跑不跑得到」。量到的关键事实：`TestDwdWriteAgainstMysql` 带 `e2e`，门禁 `-m "not e2e"` 不跑。
4. **幂等面**：双源/单源两组输入各跑两遍（逐格相等、`stats` 相等）、换 `merged_at` 再跑（**只有 `_merged_at` 这一列变**，其余 7 列相同）、帧字典顺序交换（输出相等）。
5. **直通查询面**：15 个参与对账的域逐个打「腿数 → dwd 表在不在 → 行数」。量到 `stock_action [单源]: 腿=['ths'] dwd 表=True 行数=0`、`futures_daily/index_daily/option_daily [单源] dwd 表=False`、`stock_daily [2 腿] 行数=719316`。
6. **同缺面**：两条腿逐字段的空值形状与**声明面**（`scale`、`tolerance` 各自两侧值）。它给出「今天两份 yaml 的 OHLC 容差相同 ⇒ 洞 2 尚未在真机咬过人」这个结论，也复用了 C48 面 6 的单位归因。
7. **直通查询执行面**：用**生产** `_inspect_columns` / `_key` / `build_data_select` 逐域真建真查，把「表不存在 ⇒ 404 在建 SQL 之前」与「SQL 建不出来」分开。
8. **真腿直通面**：`ods_stock_action_ths` 用生产 `ods_frame_reader` 读 → `merge_source_frames` → 与 `dwd_stock_action` 的行数对照（55,073 → 55,071，表 0 行）。

读数遍次披露（不粉饰）：`census-pass1-only-six-faces.txt` 是第 1 遍（档案 118 行 = 6 行抬头 + 112 行正文，只有面 1-6，23 条语句全 `SELECT`，脚本自身 `exit=0`）—— 它**还没有面 7 的「用生产函数真建真查」和面 8 的「真腿直通」**，也就是本轮两个洞的量化落点都缺；`census-pass2-crash-on-reflection.txt` 是第 2 遍（档案 246 行 = 6 行抬头 + 240 行正文），面 7 第一版直接用 SQLAlchemy 反射建表元数据，撞在 `sqlalchemy.exc.NoSuchTableError: dwd_bond_daily` 上**整遍崩掉**：这一遍的退出状态当时没有单独捕获（判断它不是成功遍的依据是正文以 traceback 结尾、末行没有只读闸门的语句计数，不是凭一个退出码）；崩溃本身是判据的一条读数：**404 与 500 的差别来自一个吞异常的读取**。第 3 遍把面 7 改成走**生产** `_inspect_columns`（它把一切异常吞成 `[]`，所以给出的是「表真的存在=False → endpoint 在这一步 404」而不是一个 traceback）—— 修前权威档取第 3 遍（`census-pre-fix.txt`）。三遍的 tolerance 组合数相同（4 组），第 1、2 遍不是少测了组合，是少测了两个面。

## 二、改了什么（逐条对着判据）

| 面 | 改动 | 挣到哪一格 |
|------|------|------|
| tolerance 配对 | `cross_check.py`：新增 `_stricter_tolerances(mapping_a, mapping_b)`，逐字段合并、**紧的那条生效**；`compare_source_frames` 不再一侧整本字典胜出 | \|03（②的判定分母不再取决于参数顺序；两份 yaml 声明相同 ⇒ **行为保持不变**，这是面 6 读出来的而不是假设的） |
| 歧义键处理 | `dwd_merge.py`：`_index` 从「发现重复业务键即 `raise`」改成「该键整体拒绝、其余照常建索引」，拒绝的键逐条落进新增的 `MergeStats.colliding`；**键列缺失仍然 `raise`**（列级不对齐不是行级判断能解决的） | \|08（代码面：`stock_action` 现在能交出 55,071 行。表里 0 行不因此格翻勾 —— 见 §五） |
| ①的第二种形状 | `tests/test_cross_check.py`：新增 `TestCounterexamples::test_a_mapped_column_the_frame_lacks_errors_rather_than_passing`，在生产入口 `compare_source_frames` 上断言 `missing mapped columns.*volume` | \|03 |
| 配对面 | `tests/test_cross_check.py`：新增 `test_the_tighter_tolerance_decides_whichever_leg_declares_it`，同一批输入把紧/松两条声明**正反两个方向**各比一次，两次都必须 `DEVIATION` 且 `samples[0].field == "close"` | \|03 |
| 直通落库面 | `tests/test_dwd_merge.py`：新增 `TestAmbiguousKey` 5 条（两义键被拒且其余键照常落、三义键整组拒、缺列仍 fail closed、服务层把拒绝报出来、多源胜出不算碰撞）＋ `TestPassthroughQueryFace` 2 条（直通输出列 **恰好等于** dwd 表声明列；`layer=dwd` 走生产 `_key`/`build_data_select` 建得出 SQL 且表名/键列/`limit` 都在） | \|08 的代码面（不翻勾）＋ \|09 |
| 幂等面 | `tests/test_dwd_merge.py`：新增 `TestRecomputeIdempotence` 5 条（同输入两遍逐格相等且 `stats` 相等、字典顺序不改变输出、晚一点的时钟只动 `_merged_at`、同键落两次是**同一行就地更新**、空重算什么都不写）—— 最后一条用记录型引擎（`_RecordingEngine`/`_RecordingConnection`）取真实 upsert 语句：`ON DUPLICATE KEY UPDATE` 在位、**键列出现在 INSERT 列表里但不出现在 UPDATE 集里**、值列与追溯列都在 | \|09 |

未改的东西（刻意列出，避免被读成放水）：判据原文一字未动（§2 行是内容哈希出来的 ledger key）；`--cov-fail-under`、`exclude_lines`、ruff/mypy 规则、门禁成员清单都没有碰；`_index` 的 fail-closed 语义在「列缺失」这一支保持原样；两份 yaml 的 `tolerances` 一字未改（本轮改的是**读取方式**，不是容差值）。

## 三、判定面：每个断言都能被反证，且反证由机器执行

三格探针（`scripts/quality/acceptance_item_probe.py`）+ 反事实共 **21 条**，`--self-test` 全绿：

```
OK: 30 probe(s) measured; every judge is reachable from its declared repair, and all 229 counterfact(s) flip a clean reading back to a gap.
```

| 条目 | 判据点 | 反事实（每条都必须把干净读数打回 gap） |
|------|------|------|
| \|03 | 4 个反例节点全绿 + 两条反例都走生产入口 `compare_source_frames` + ①的 `match="missing mapped columns"` + ②点名字段 `volume` + 旧的整本字典配对已移除且逐字段合并 + 合并取紧 + 正反两向都量 | 一条反例变红 / 节点被改名（跑不满条数）/ 字段名不匹配只测下游 helper / 不再要求报错 / mismatch 断言不点名字段 / tolerance 配退回一侧整本字典 / 合并不再取紧 / 只量一个方向 —— **8 条** |
| \|08 | 6 个直通节点全绿 + 逐键拒绝在位 + 旧整窗 `raise` 文案已移除 + 拒绝的键可报告（`MergeStats.colliding`）+ 键列缺失仍 fail closed + 查询面走生产 `_key`/`build_data_select` + **`dwd_stock_action` 有行**（读自 `census-post-fix.txt`） | 一条节点变红 / 节点被改名 / 退回整窗拒绝 / 旧文案又出现 / 拒绝不再报出 / 缺列不再 fail closed / 查询面不走生产函数 / **代码面全通但表里 0 行** —— **8 条**（最后一条正是当前真实读数） |
| \|09 | 5 个重算节点全绿 + 键集排序 + 写侧走 `build_upsert_sql(table, columns, key)` + dwd 表 `PRIMARY KEY` 即业务键 + 空重算什么都不写；真库 e2e 那一格只披露读数（`exit=5`，门禁不跑） | 一条重算节点变红 / 节点被改名 / 键集不再排序 / 写侧退回追加 / 主键不再是业务键 —— **5 条** |

判据**没有被放宽**的两处自证：

- \|03 的两条反例断言的是**具体列名与具体 verdict**，且 `entry_is_production` 要求两个见证方法体里都出现 `compare_source_frames(` —— 只测 `normalize_frame` 或只测 `_disagreeing_keys` 不算，因为校对作业读的是那个入口。
- \|08 拒绝在「代码面全通」时翻绿：`landed_rows > 0` 是判据的一条，读数取自档案而不是现场猜的（`_reading_after` 只解析 `census-post-fix.txt` 里那一行；文件不在、marker 不在、数字不在都读成 `-` ⇒ `number()` 给 -1 ⇒ gap）。本轮的 gap 判词直接写出缺的是哪一次动作（经确认的落库写入），而不是把口径改成「merge 能跑就算」。

手工反证（不依赖探针）：把 `_stricter_tolerances` 换回旧的 `or` 配对、把 `_index` 换回整窗 `raise`，两条新用例分别红在 `assert summary.verdict is Verdict.DEVIATION`（修前是 consistent）与「整窗不交」上；恢复后 43 passed / 1 deselected（`witness-pytest.txt`）。

## 四、门禁与档案

| 遍次 | 档案 | 它验证了什么 / 为什么被取代 |
|------|------|------|
| 探针 --self-test | `probe-self-test.txt`（`SELFTEST_EXIT=0`，30 探针 / 229 反事实） | 判定面咬得住，且 repair 面可达 |
| 探针 三格 | `probe-items-three-prebackfill.txt`（`PROBE_EXIT=0`；\|03 proven、\|09 proven、\|08 gap） | 本轮三格的逐格读数（回填前） |
| 探针 --all | `probe-all-prebackfill.txt`（`PROBE_ALL_EXIT=0`；`census gap=10, proven=20`，无 drift） | 新增两面没有把已转正的格子碰坏（AC-9\|02/\|04/\|05、AC-13\|07 仍 proven） |
| census 修前/修后 | `census-pre-fix.txt` / `census-post-fix.txt` | 修前 = 「先量」的证据；修后 = 判据引用的权威读数（\|08 的 `landed_rows` 从这里读） |
| 见证子跑 | `witness-pytest.txt`（43 passed / 1 deselected，**带 --no-cov，不是门禁读数**） | 单元面绿 |
| gate 回填前第 1 遍（**红**） | `gate-run1-prebackfill-attempt.txt`（487 行 = 20 行抬头 + 467 行正文，`GATE_EXIT=2` 读自文件内部，只有 8 段 `===== gate:`） | 红在 a2-check 的 mypy 面：本轮把 `_index` 的返回形状从 dict 换成 `(indexed, colliding)`，`docs/evidence/C48/cross_check_census.py` 还在把返回值当 Mapping 用（3 处 arg-type/attr-defined）。因为 gate 在这一段就中止，**pytest / 覆盖率 / 前端三段的读数以这一遍为准是不成立的** —— 这也是把它单独留档而不是直接重跑了事的原因：它记的是「判定面搬家会咬到谁」 |
| a2-check 定点复验 | `a2-check-after-c48-instrument-fix.txt`（390 行，`A2_EXIT=0`，`A2 files: 371` 四面全 ok） | 只跑这一个成员，先在最小面上把「改读数那一边就够了」证死，再花一遍完整 gate（a2-check 会扫 `docs/evidence/**/*.py`，所以这台仪器必须在 A2 面达标） |
| gate 回填前第 2 遍（绿） | `gate-run2-prebackfill.txt`（7,369 行 = 14 行抬头 + 7,355 行正文，17 段、`GATE_EXIT=0` 读自文件内部） | 描述**回填前**的最终树：3236 passed / 6 skipped、`TOTAL 11636 1086 2874 288 89.28%`（阈值 84%）、vitest 109 passed（14 files）、Playwright 18 passed |
| gate 回填后 | `gate-run3-postbackfill.txt` | 翻两格 + §0/§10 + 台账 JSON 落地之后那一遍；读数以它自己的正文为准（抬头不预填数字） |
| gate 最终树 | `gate-run4-finaltree.txt` | README 与 run3 档案落地后、描述最终树的那一遍；抬头写明与上一遍的差别与被取代原因（`evidence-traceability` 会扫 `docs/evidence/**/*.py`，任何跑完再写的档案都会使上一遍不再描述最终树） |

四遍而不是两遍，是这一轮实测出来的代价（红一遍 + 回填前后各一遍 + 最终树一遍）；每一遍都完整未裁剪留档，取代关系写在上面这一列而不是靠删掉旧的。

`ac9_residual_census.py` 自身在本轮过 A2 面：`RUFF=0`、`ruff format --check=0`、`MYPY=0`（a2-check 会扫 `docs/evidence/**/*.py`）。`.env` 与任何密钥值全程未被读取或打印；真库操作只到 `SELECT`/`SHOW` 与 `information_schema`。

## 五、本轮没有做的事 / 须用户决定的面

1. **\|08 不勾，且这不是判据太严**。三件事按危险度递增列出，都需要用户确认或另行决定：
   - `dwd_stock_action` 落库：直通现在能算出 55,071 行，落进去是**一次生产数仓写入**（`DwdWriter.write` 的 upsert）。本轮按既有规则不动它。
   - 那个两义键本身：`(603883, 2024-06-27)` 两套方案，表的键是 `(symbol, ex_date)` ⇒ **契约容不下这件事**。要么加宽键（需要 DDL 与迁移，且要定「方案」字段怎么取），要么维持逐键拒绝并把它当成上游数据形状的一条已知限制（当前状态：拒绝并且报得出来）。
   - `dwd_futures_daily` / `dwd_index_daily` / `dwd_option_daily` **表不存在** ⇒ 这三个单源域的 `layer=dwd` 现在就是 404。`ddl.dwd_table_ddl` 对任何已注册域都渲染得出 DDL，但**生产里没有任何调用点应用它**（只有测试调用）。建表是 DDL，须确认。
2. **`batch_watermark` 从来没被写过**（本轮面 5 顺带读到 0 行，而 `dwd_stock_daily` 有 719,316 行）：C38b 的归档已把这张表定性成「会被按 domain 清掉的弱审计面」。它不属于 \|08 的判据，登记为独立缺口。
3. **不计入本轮的任何东西**：`AC-9|06`（`_diff_flag` 打标正确 / `source` 留痕 / `_as_of` 写入）仍需真机 dwd 对账；`AC-9|07`（修订传播）仍无判定面；`AC-9|01` 仍是「两侧都有 mapping 的域只有 1 个」。三格本轮**没有碰**。
4. 登记给 C50 的候选：把 `acceptance_item_probe.py` 做成门禁成员（本轮 21 条反事实全部只在手工遍次里被验证过，绿色门禁对它无感）；`PARTITION_MAINTENANCE` 的执行器（DDL 门）；akshare 退路成交额零值与混合单位（C48 §一-6 归因的修数据半边）；`--all` 里剩下的 10 格 gap 逐格归因；**`docs/evidence/**.py` 这些历史仪器对私有 API 的依赖面** —— 本轮第 1 遍 gate 就是红在这里（`_index` 换返回形状咬到 C48 的 census 件），改产品私有函数前没有一个成员会告诉你有几台仪器在读它。

## 六、本轮 diff 面

- `opendata/pipeline/cross_check.py`：`_stricter_tolerances` 新增 + 配对点改一处。
- `opendata/pipeline/dwd_merge.py`：`_index` 逐键拒绝、`MergeStats.colliding` 新增、调用点与 docstring 的 Raises 段同步。
- `tests/test_cross_check.py`：`TestCounterexamples` 2 条（15 → 17）。
- `tests/test_dwd_merge.py`：`TestAmbiguousKey` 5 + `TestPassthroughQueryFace` 2 + `TestRecomputeIdempotence` 5 + 记录型引擎 2 个桩 ⇒ 用例 15 → 27（新增 12 条；门禁子跑 26 passed / 1 e2e deselected）。
- `scripts/quality/acceptance_item_probe.py`：三组常量（节点清单/档案路径）+ `_reading_after` + 3 组 measure/judge + 3 条 `Probe` 注册（27 → 30 探针，反事实 208 → 229）。
- `docs/迭代计划/迭代1-重构数据中台/验收文档.md`：v5.21 修订行 + §2 两格翻转（\|03、\|09）+ §10 AC-9 台账行与条目级 k/m（3/9 → 5/9，未逐条达标标记保留）。
- `docs/quality/acceptance-item-ledger.json`：两格 `proven` 全字段（round/date/command/evidence/note），\|08 写 `gap` + `reason`（**不是留在 `unreviewed`** —— 已经量过并判红，与「还没看」必须是两种状态；`acceptance_ledger_check` 对 `gap` 要求非空 reason，这一条正是给它的）。
- `docs/evidence/C48/cross_check_census.py`：`_index` 调用点显式解包 `rows, refused`，并把「重键被拒」并进它本来就打印的那一行 —— 本轮形状搬家带出来的**读数面**修改。C48 的归档读数（`cross_check-census-*.txt`）一件未动；那台仪器重跑时的输出会多一列「重键被拒」，这与 C48 留档不同是预期的（留档描述的是 C48 那一刻的树）。
