# C58 —— AC-8 的 8 格里有 4 格不需要仓库写就能判：把它们做成可证伪条目，并顺手修掉 C57 那条 03:00 cron 的一处分寸缺陷

本轮判据面全部落在 `opendata/`、`tests/`、`scripts/quality/` 与**真仓库的只读面**上；
没有执行任何 `ALTER` / `INSERT` / `DROP`，没有读取或打印 `.env` 与任何密钥值。

## 1. 入口：八格里四格逐字是 `{"state": "unreviewed"}`

C57 之后 `AC-8` 还剩六格 `unreviewed`（|01/|02/|03/|04/|07/|08）。其中四格**不需要生产写入**就能判：

| 格 | 判据原文点名的面 | 本轮判定落点 |
| --- | --- | --- |
| \|01 | 命名 `ods_<domain>_<source>` + 源原始列 + `_source/_fetched_at/_batch_id` + 业务 key 主键 | 派生点/渲染点/真仓库逐表读数 三面 |
| \|02 | 仓库 DDL 由独立 alembic 环境管理；应用启动不建表（单测/集成验证） | 通路四读 + 启动面 + 炸弹桩单测 |
| \|03 | upsert key 级幂等（价格修正不产生重复行，**单测**） | 判据点名的单测面 5 格 + 语句三面 |
| \|07 | 无 `id` 列的 ods 表在 `/tables` 分页可用 | 排序列回落链 + SQLite 真翻页 + 端点 HTTP 用例 |

`|04`（写入基准含内存）与 `|08`（真机双源落 ods）要的是**真写入**，本轮不动（见 §6）。

## 2. 顺手修掉的真实缺陷：census 读的是还没填的注册表〔源码〕

`maintain_partition_horizon(tables=None)` 用 `alert_matrix.warehouse_tables()` 展开表集合，
而它读的是 `data/registry.py:384` 的 `get_registry()` —— 一个 `@lru_cache(maxsize=1)` 的**空表**，
直到 `opendata.data.providers.register_providers()` 被调用才填入 33 条能力。
`_resolve_fetcher`（增量任务路径）本来就"先注册再读"，但**03:00 那趟 cron 不经过它**：

- 后果：在新进程里 census 只有 20 张 `dwd_*`，**一张 `ods_*` 都不进维护面** ——
  而巡检每天写入的正是 ods 层（`ods_stock_daily_ths` 13 个分区 / `ods_stock_action_ths` 37 个分区）。
  分区落后不报错（缺的年落 `pmax`），所以这条缺陷不会有任何异常，只会让表慢慢不能裁剪。
- 修法：census 展开点先 `register_providers()` 再读（`opendata/pipeline/jobs.py`）。
- 后防两格（都会红）：`test_the_default_census_is_read_after_the_providers_are_registered`
  断言"注册"发生在"读 census"**之前**（把注册删掉即红）；`test_the_registered_census_reaches_the_ods_layer`
  用**真注册表**断言 census 里必须有 ods 表、且 `dwd` 部分恰等于全部已知域（census 丢层即红）。
- 为什么 C57 没抓到：那轮的 `test_the_default_table_set_is_the_registered_warehouse` 用 monkeypatch
  **把 `warehouse_tables` 打了桩**，桩返回值恰好是"两条腿都在"的样子 —— 桩替掉了唯一会暴露缺陷的那次真读。

## 3. 仪器更正：同一处惰性让 C57 的真仓库读数欠报〔档案 `ods-face.txt`〕

`FACTS registered=53 ods_registered=33 legs=18 live_ods=3 ods_named=3 ods_trio=3 ods_key_pk=3
ods_autokey=0 partitioned=9 gap_tables=8 gap_partitions=8 pmax_rows=0 current_year=2026 years_ahead=2
ctrl_warehouse_tables=0`

| 读数 | C57（裸进程） | C58（注册后） | 差值来源 |
| --- | --- | --- | --- |
| census 表集合 | 20 | **53**（dwd 20 + ods 33） | 33 条 ods 腿此前根本不在集合里 |
| 当场已分区 | 6 | **9**（6 dwd + 3 ods） | 同上 |
| 年度上界缺 `p2027` | 5 | **8**（5 dwd + 3 ods） | 同上 |
| `pmax` 行数合计 | 0 | **0** | 未变 ⇒ 待补的 `REORGANIZE` 只动元数据〔推断，基于档案读数〕 |

分区面逐表读数〔档案〕：9 张分区表里除 `dwd_stock_adjust`（上界 `2029-01-01`）外**全部缺 `p2027`**，
`ods_stock_daily_akshare` 的分区列是 **`日期`**（中文列名，源侧原始列名原样保留的必然结果），
其余按域为 `trade_date` / `report_period` / `as_of` / `ex_date`。
对账两面：注册表里尚未建出的 **44** 张；未注册却存在的 **0** 张。

`AC-8|02` 的真仓库侧同份档案：控制库 `alembic_version`（schema `opendata`）= `['0003']`，
仓库 `alembic_version_data`（schema `opendata_data`）= `['0005_dwd_stock_adjust']`，
控制库里的 `ods_/dwd_` 表 **0** 张。

## 4. 四格各判什么〔源码 + 档案〕

- **\|01**：`domains.ods_table()` 是全库唯一派生点；`ods_table_ddl` 用 `[*columns, *ODS_METADATA_COLUMNS]`
  **无条件**追加三元组；`ddl.py` 的生成器里 `AUTO_INCREMENT` 出现 0 次、主键由 `PRIMARY KEY ({key_list})`
  从业务 key 渲染、key 缺失即 `"needs a business primary key"` 抛错。真仓库逐表：命名 3／三元组 3／
  业务 key 主键 3／带自增 id 0。"注册 33 条 ods 腿 vs 仓库 3 张表"的差额**披露给 `|08`**，不当成本格的分数。
- **\|02**：`script_location` 分叉（逐字取回而不是只断言"不同"）、仓库 `env.py` 的 `target_metadata = None`
  （结构上无从 generate 出 ORM 表）、URL 只取 `settings.data_database_url`、`VERSION_TABLE = alembic_version_data`
  且控制侧 env 里连 `data_database_url` 都不出现；启动面用 `ast` 走查 `lifespan` 里 `create_tables()`
  调用点**恰好一处**且在 `is_production` 守卫的 else 分支，另有一格把 `data_engine` 换成
  `begin()/connect()` 即抛 `AssertionError` 的炸弹桩后跑**真实** `create_tables()`。
- **\|03**：判据原文点名"单测"，判定就在那一面（5 格节点全绿），语句三面各自承重
  （`AS new` 别名式 / `if column not in set(key)` 把 key 挡在 UPDATE 之外 / 空列表 fail closed 在**两条** builder 里各计一次）。
- **\|07**：回落链 `id` → 业务主键 → 首列、`LIMIT :limit OFFSET :offset` 走绑定参数、无列可排即拒、
  端点函数体先 `table_shape(` 再 `page_sql(`、模块内硬编码 `ORDER BY id` 回潮 0。

## 5. 一格只披露、不判定〔档案 `probe-items-ac8.txt`〕

`AC-8|03` 的真库那格 `TestLiveUpsert::test_corrected_value_updates_in_place_without_duplicating`
是 `@pytest.mark.e2e`，本轮 `-m "not e2e"` 的选择式不跑它 —— 探针把它读成 **`exit=5`**（一条都没选中）
而不是"跑了但失败"，并**显式打印**这一条。本格判据落点因此是单测面；
"修正后不产生重复行"在真 MySQL 上仍欠一次经确认的真跑。

## 6. 本轮没做的事（都要用户点头，不是我能自行裁定的）

1. **没有 `REORGANIZE`**。8 张表缺 `p2027`，`pmax` 行数为 0 ⇒ 只动元数据；但这是生产仓库的 DDL，
   等确认。副作用同 C57：`dev` 启动即注册这条 03:00 cron，到点会自己补。
2. **没有跑 e2e**（跨年落分区 / 真库 upsert 就地改），它们在工作库里 `CREATE/DROP` 探针表并写行。
3. **`|04` 未做**：写入基准要"全量落库耗时与**内存**"，需要一次真写入 + RSS 采样。
4. **`|08` 未做**：真机双源落 ods。档案读数是它的规模面 —— 注册 33 条 ods 腿里**只有 3 张表存在**
   （另有 14 张注册 `dwd_*` 也未建出），建表 + 落库都要确认。

## 7. 登记三条自身欠账

- **一次越界的执行**：复算时漏写 `-m "not e2e"`，真在生产仓库里建删了两张 `_probe_*` 临时表。
  事后复核与不兑换成 `|06` 证据的理由见 §11。

- `AC-8|05/|06` 的真仓库面常量仍指 `docs/evidence/C57/partition-horizon.txt`（读数 20/6/5）。
  repoint 到本轮更正后的档案会改 judge 输入 ⇒ 必须重跑一次全量自测才配得上档案，留待下轮。
  两格结论 `gap` 不受影响，方向是"落后规模被欠报"而非"被高报过关"。
- `AC-8|05` 判据原文写"按 `RANGE COLUMNS(trade_date)` 年分区"，而仓库按各域业务日期列分区。
  措辞是否加宽属产品决定，本轮只量不改（C57 已登记同一事实）。

## 8. 复算命令

```bash
bash docs/evidence/C58/run_ods_face.sh            # 真仓库只读面（可反复跑）
bash docs/evidence/C58/run_probe_c58.sh items     # 四格单格复算
bash docs/evidence/C58/run_probe_c58.sh self-test # 47 格全量反事实自测
PYTHONPATH=. python -m pytest tests/test_pipeline_jobs.py tests/test_database_functions.py \
  tests/test_warehouse_ddl.py tests/test_ods_writer.py tests/test_warehouse_migrations.py \
  -q --no-cov -m "not e2e"                        # 125 passed / 6 deselected

> **这条命令里的 `-m "not e2e"` 不是排版**：`tests/test_partition_maintenance.py` 与
> 部分 ods writer 用例的 fixture 直连 `settings.data_database_url`，少了这个选择式就会在
> 生产仓库建删临时表（本轮真犯过一次，见 §11）。`--no-cov` 同理：部分跑不要写 coverage 数据。
```

## 9. 本轮的仪器与格式面

- 探针 43 → **47 条**，反事实 345 → **378 条**（四格各 11／10／5／7），全量自测 `PROBE_SELF_TEST_EXIT=0`：
  每条 break 都把干净读数打回 gap，每个 judge 都能从自己声明的 repair 到达。
- `--sync-faces` 后 `docs/quality/acceptance-probe-faces.json` 的 diff 恰为新增四行。
- 台账 `proven` 39 → **43**（`gap` 11、`unreviewed` 80 → **76**）；文档 §2 四格勾选、§10 `条目级 0/8 → 4/8`。
- **格式化欠账（逐个测过）**：本轮触碰的五个文件里 **四个在 HEAD 上即 `ruff format --check` 不过**
  ——`opendata/pipeline/jobs.py`、`scripts/quality/acceptance_item_probe.py`、`tests/test_pipeline_jobs.py`、
  `tests/test_partition_maintenance.py`；只有 `tests/test_database_functions.py` 本就合式。`a2-check`
  的比较对象是「自 A0 基线 `4ee1bbc` 起改动过的文件＋工作树」，C57 的门禁遍没覆盖到最后一次编辑 ⇒ 债今天才咬人。
  `tests/test_partition_maintenance.py` 本轮并非主动要改它，而是它已经红在门禁里：加 `# fmt: off` 能绕过，
  但那正是 C35 收口过的「门禁空转」，所以选择格式化。〔档案 `probe-items-ac8.txt`〕格式化前后四格
  判定读数逐字一致（`===== 正文` 之后各 45 行、两遍 `PROBE_ITEM_EXIT=0`）。
  **由此产生一条行号更正**：落点断言 `assert placements == ["p2027"]` 从 `tests/test_partition_maintenance.py:147`
  移到 **:145**，于是 C57 README 与验收文档 §0 v5.29 行里的 `:147` 引用今天起差两行。断言文本一字未动，
  历史档案不回改，更正记在这里。
- **mypy 债（本轮收口，归因 C56/C57）**：`make a2-check` 今天报 7 条 mypy 错，全部是前两轮攒下的——
  `scripts/quality/acceptance_item_probe.py:554`（`map_entry_field` 里 `ast.Assign` / `ast.AnnAssign`
  双分支赋值没注解）、同文件 `4190/4194/4195/4199/4200`（C57 的 `measure_ac8_05` 用
  `bool(m) and m.group(...)` 解引用 `Match | None`）、`scripts/ops/qfq_official_check.py:146`
  （C56 `e803dc2` 的动态 import 返回 `Any`）。修法都是**只动类型不动语义**：注解抄同文件
  `set_literal_members` 里已经写对的 `targets: Sequence[ast.expr] = ()` / `literal: ast.expr | None = None`；
  五处改成 `m.group(...) if m else ""`（token 不可能 `in ""`，读数为 None 时仍 False，与 `bool(m) and` 等价）；
  一处改成 `cast("Callable[..., Any]", ...)`（引号形式让 ruff 的 TC003/TC006 与 mypy 同时满意）。
  现在 `ruff check`／`ruff format --check`／`mypy`／`bandit` 四项在 387 个 A2 文件上全绿。
  **为什么现在才红**：C56 与 C57 都没有跑门禁遍，这条和上一条格式债是同一个根因。
- **本轮自己的文档编辑缺陷（已修）**：给 §0 的 v5.30 行追加 ⑨ 时用了 `rstrip("\n")`，把该行末尾换行一起吃掉
  ⇒ 文档 397→396 行、`---` 分隔线紧贴表格最后一行；**探针随即把 §2 的 `AC-8|01` 报成 document line 171**（应为 172），
  也就是仪器把我的排版事故读成了数据。补回空行后重跑四格，正文与格式化后那一遍逐字一致
  （`document line` 回到 172/173/174/178）。教训：这个文档的行号是探针的输入，改它必须用
  `splitlines(keepends=True)` 并在写完后断言行数不变。
- **最终档案跑的就是将要提交的源码**：`probe-self-test.txt` 这一遍读数为
  47/47 个探针走到判定、378 条 break 各施加一次且都把干净读数打回 gap、`PROBE_SELF_TEST_EXIT=0`，
  跑在我最后一次编辑（mypy 三处类型修法）之后。〔档案 `probe-self-test.txt`〕

- **仪器的第二处自身缺陷：名字遮蔽**。`measure_ac8_02` 里有个局部变量叫 `main`，把模块级函数 `main()` 遮蔽了；
  moment 面是按「measure 函数的传递闭包里出现 `"git", "status"` 字面量」登记的（`surfaces_in_closure`），
  于是这一格被登记成一个它**根本不读**的工作树面。改名 `app_src` 后 `--sync-faces` 读数为
  47 个探针 / **12** 个在读 moment 面（与 HEAD 的 12 同数，新增四行全是 `{"moment_faces": []}`，
  没有任何旧行被改动），四格正文重跑逐字不变。教训与 C40、以及本节上一条同向：
  闭包扫描是**名字敏感**的，读数说「这格在读工作树」时先看它到底调到了什么。

## 10. 档案表

| 文件 | 内容 | 证据档 |
| --- | --- | --- |
| `ods-face.txt` | 真仓库只读面：注册后 census / 对账 / ods 形状 / 分区面 / 两套版本表 | 〔档案〕 |
| `run_ods_face.sh` | 上面那份读数的生成脚本（只读三类语句） | 〔档案〕 |
| `probe-items-ac8.txt` | AC-8\|01/\|02/\|03/\|07 四格复算，含四行 VERDICT 与 \|03 的 `exit=5` 披露 | 〔档案〕 |
| `probe-self-test.txt` | 47 格全量反事实自测 | 〔档案〕 |
| `run_probe_c58.sh` | 探针复算 / 自测的 wrapper（`items` / `self-test` 两面） | 〔档案〕 |
| `README.md` | 本文件：源码面为〔源码〕、代价与口径推理为〔推断〕 | — |

## 11. 一次越界的执行（如实登记，不转成证据）〔档案 + 事后只读复核〕

复算 §8 那条 5 模块单元命令时，我在同一个 shell 里追加了一次
`pytest tests/test_partition_maintenance.py -q --no-cov`——**漏掉了 `-m "not e2e"`**。
`TestCrossYearWrite` 的 fixture 是 `create_engine(settings.data_database_url)`，也就是
**生产仓库 `opendata_data`**，于是那 3 条 e2e 真的跑了：在真库里
`DROP TABLE IF EXISTS` + `CREATE TABLE _probe_partition_maintenance`（自带 `p2024`+`pmax`）、
`ensure()` 补出 p2025/p2026/p2027、插入 2027-03-01 那行、断言落点 = `["p2027"]`、
再建删一张 `_probe_unpartitioned`。9 passed 里包含这 3 条。

这违反了本轮一直遵守的一条：**生产仓库的写/DDL 需经确认**。事后只读复核〔当前仓库状态〕：

- `_probe*` 残留表 **0 张**（teardown 的 `DROP TABLE IF EXISTS` 生效）；
- 9 张真实分区表的 `pmax` 全部仍是 `MAXVALUE`、上界与 `ods-face.txt` 逐字一致 ⇒ **没有对任何业务表发过 `REORGANIZE`**；
  `ensure()` 每次只接 `self.TABLE`（即 `_probe_*`），没有表名回落到业务层的路径；
- 数据面：本次只建删了两张临时 probe 表，未 INSERT 任何业务表。

**为什么不用它翻正 `AC-8|06`**（这是这一节存在的全部理由）：那一格的判据要的是
「一次经确认的跨年真跑留档」＋「真仓库那一年不缺」两面。机制面这次确实证明了
`ensure` + 落点断言能工作（且断言一字未改），但它是**流程越界的产物**：如果我把它写进档案、
把 `ran_live` 挣成 yes，就等于用一次未经许可的执行去兑换一格 proven——那正是 C35 收口过的
"结果对、来源错"。所以 `|06` 仍 `gap`、`docs/evidence/C57/` 与 `docs/evidence/C58/` 的档案
里都没有这次运行的读数；机制面的这条新信息只登记在本节。

**仪器的对策**（防我再犯）：`-m "not e2e"` 不能只靠人记得写。本轮把这一条当作 #70 之外新增的
登记项交给下一轮——给 `tests/` 的仓库侧 e2e 加一道"未被显式环境变量放行即 skip"的闸门
（与 `tests/test_port_fidelity.py` 现有的 em 网络 skip 同一形状），并把 `make gate` 的
`-m "not e2e"` 与这条闸门对账。本轮没有实现它：那要改 `tests/` 与 `Makefile`，而本轮的
判据面已经动过 `tests/`，在同一条 03:00 cron 的缺陷修复里再叠一道全局闸门会把两件事的回归面混在一起。

