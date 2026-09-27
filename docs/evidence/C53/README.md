# C53：三类域级口径成为口径映射表的必填字段并被代码读取（AC-9\|01 的「承载」半翻正，「覆盖」半仍 gap）

判据原文（§2 L177；本轮在变更日志插入 v5.25 一行后该条目位于 L178，判据文字一字未动）：
**口径映射表**存在且覆盖 P0 域（字段映射/单位换算/复权口径/key 规范化/停牌语义/差异率分母）。

C52 把这格量成 gap，读数说缺两半：

* **覆盖**：P0 域 3/5，`financial_statement`/`financial_indicator` 两条腿不在这张表里；
* **承载**：六类口径里 `复权口径/停牌语义/差异率分母` 三类**结构上进不了这张表**
  （`_FIELD_KEYS = {from, scale, normalize, ms_column}` 是逐列键集，域级约定无处落）。

本轮把第二半做实：三类口径成为**域级必填字段**，加载器缺一项就拒绝加载，两个真正
按它行事的缝（查询层复权合成、双源校对可比性）各自去读它；每个声明值都注明它靠
哪条证据成立，量不出来的显式写 `unmeasured` 并让读取缝拒绝，而不是猜一个填上。
第一半（两个 financial 域）本轮**没有**做完，`AC-9|01` 仍是 gap——理由与补齐路径见
§8，登记为 #66 的剩余面与两条新面。

探针读数（`probe-ac9-01.txt`，逐字）：

```
- 六类口径落表: 字段映射=yes、单位换算=yes、复权口径=yes、key 规范化=yes、停牌语义=yes、差异率分母=yes
- P0 覆盖: 3/5 个 P0 域在这张表里；没覆盖的是 financial_indicator, financial_statement
VERDICT AC-9|01: gap — …未覆盖的 P0 域：financial_indicator, financial_statement…
```

---

## 1. 改动面（一张表说清「谁声明、谁读、谁拒绝」）

| 面 | 文件 | 本轮变成什么 |
| --- | --- | --- |
| 声明结构 | `opendata/data/mapping.py` | 新增 `_ADJUST_BASES`/`_SUSPENSION_SHAPES`/`_DENOMINATORS`/`_DOMAIN_KEYS`/`_CALIBERS`；`DomainMapping` 把 `adjust`/`suspension`/`denominator` 变成**必填字段**；`_parse_domain` 走 `_require_caliber()`：缺一项、取值不在枚举里、多一个表不认的键，三样都是 `RuntimeError` |
| 导出 | 同上 `mapping_as_json()` | 三类口径随域一起导出（读数 `caliber-export-face.txt` 第 1 面） |
| 读取缝 1 | `opendata/pipeline/query.py:253-256` | `apply_adjust_to_rows()` 先 `require_adjust_basis(domain)`；声明不是 `unadjusted` 就 `RuntimeError`，不再靠调用方自觉 |
| 读取缝 2 | `opendata/pipeline/cross_check.py:171` | `compare_source_frames()` 在归一化**之前**跑 `require_comparable_calibers(a, b)`：分母必须两侧都是 `key_union`、停牌形状必须一致且不能是 `unmeasured` |
| 表内容 | `opendata/data/mappings/ths.yaml`、`akshare.yaml` | 每个域补三行声明，且每行上方注明取值来源（实测/上游契约/定义/待测） |
| 仪器 | `scripts/ops/suspension_shape_check.py` | 参数化 `--domain/--sources`（原来写死 `stock_daily` 两腿），使停牌形状可按腿复量；**本轮首次入库**（C52 之前它一直是未跟踪文件，见 §7） |
| 用例 | `tests/test_data_mapping.py`（+`TestDomainCaliberDeclarations`/`TestCaliberReaders`）、`tests/test_cross_check.py`（+`TestCaliberGate`）、`tests/test_data_query.py`（+2 例）、`tests/{test_cross_check_service,test_pipeline_templates,test_dwd_merge}.py`（手工构造的 `DomainMapping(` 站点补三项声明） | 判据的六个「会坏」面各有用例钉住 |

## 2. 声明值 ↔ 证据：逐条给出「这条声明凭什么」

七条腿（`domain, source`）的全部声明见 `ods-leg-census.txt`（库内 12 张表精确
`COUNT(*)` + 每条腿的 ods 落点是否存在）。按证据种类分四档，档位不同就写不同档位，
不把定义冒充实测：

**(a) 实测（ods 行形状量出来的）** — `stock_daily` 两腿的 `suspension: absent_row`

| 腿 | 读数（`suspension-shape-stock-daily.txt`） | 判定 |
| --- | --- | --- |
| ths | 200 个抽样标的、475,402 行；`zero_close=0`；落在自身 `[first,last]` ∩ 本腿市场日（2,429 天）里的缺行 **8,240 天 / 139 个标的**；`adjusted` 列distinct 只有 `['none']` | 停牌日**整行不发** ⇒ `absent_row`；上游自报基准 `none` ⇒ `adjust: unadjusted` |
| akshare | 200 个抽样标的、19,265 行；`zero_close=0`；缺行 **55 天 / 10 个标的**；ods 表**没有** `adjusted` 列 | `absent_row`（与 ths 同形，所以两腿的 missing 可以互相比对） |

`no_trade=38`（akshare 腿「价>0 而量、额同为 0」）**没有**被写成停牌语义：那是 C24
已归因的 sina 退路缺额度缺陷，登记为待修面（§8）。把已知缺陷读成源语义，等于用一张
表去掩盖另一张表的洞。

**(b) 上游契约（不是价格行量的，是取数参数与库结构的合取）** — `akshare.stock_daily` 的 `adjust: unadjusted`

`opendata/data/providers/akshare/models/stock_daily.py:23` 的查询模型写着
`adjust: str = ""`（默认不复权，模块 docstring 同调），而 `ods_stock_daily_akshare`
里没有 `adjusted` 列可声明别的基准 ⇒ 交付的就是不复权价。README 把这档单列，是因为
它和 (a) 不同源：它证明的是「上游给的是原始价」，不是「行里量出了什么形状」。

**(c) 定义性（该域按构造没有这个约定）** — `suspension: not_applicable`

* `ths.stock_action`、`akshare.index_constituent`：都不是 K 线腿（一个是分红/除权事件、
  一个是成分权重），「停牌日长什么样」这个问题在这两个域里没有 referent；
* `ths.index_daily`：指数没有停复牌状态（停牌是个券的属性，不是指数的），且这一腿
  在库里连 ods 表都没有（`ods_index_daily_ths` exists=no）；
* `ths.stock_daily` 的 `adjust: unadjusted` 同时属于 (a)：`SELECT DISTINCT adjusted`
  只有 `none`，与 1,032,323 行因子表（`dwd_stock_adjust`）「原始价 + 因子」的 D10 口径一致。

**(d) 待测占位（有语义但没有行可量）** — `ths.futures_daily`、`ths.option_daily` 的 `suspension: unmeasured`

两条腿在库里没有 ods 表（`ods-leg-census.txt`：`ods_futures_daily_ths`/
`ods_option_daily_ths` exists=no、rows=0；`suspension-shape-no-ods-legs.txt` 是同一
事实的另一种读法：同一仪器跑这两个域，MySQL 直接 `(1146, "Table … doesn't exist")`）。
没有行就不能声称量到了形状，所以填 `unmeasured`，并**给它一个会拒绝的读者**：
`require_comparable_calibers()` 看到 `unmeasured` 就 `RuntimeError`（混合时先报
「两腿停牌形状不同」，两侧都是 `unmeasured` 时报「没有实测停牌形状，比对被拒绝」）。
这样占位符不会被下游当成「已经量过」。若哪天有人想给这两条腿编一个 `absent_row`，
加载器允许（枚举里有），但没有任何代码读它——它是表里的一个待办，不是一个事实。

## 3. 六类口径现在分别落在哪里

| 口径 | 落点 | 读数证据 |
| --- | --- | --- |
| 字段映射 | 表内 `fields.<name>.from` | `caliber-export-face.txt` 第 2 面（`{"from": "成交量", …}`） |
| 单位换算 | 表内 `scale` | 同上：ths `volume scale=1.0`、akshare `scale=100.0`（手→股） |
| 复权口径 | 表内域级 `adjust` → `require_adjust_basis()` 读 | 第 1 面 + `caliber-reader-callsites.txt` |
| key 规范化 | 表内 `normalize: plain` | 第 1 面 fields 计数 + `tests/test_data_mapping.py::TestNormalizeFrame` |
| 停牌语义 | 表内域级 `suspension` → `require_comparable_calibers()` 读 | 第 1 面 + §2 实测两腿 |
| 差异率分母 | 表内域级 `denominator=key_union` → 校对层读 | 第 1 面 + `DiffSummary.compared_keys`（键并集为分母） |

`_FIELD_KEYS`/`_DOMAIN_KEYS` 的分工也在这里说清：逐列的口径进 `fields`，逐域的口径进
`domains`；`_DOMAIN_KEYS` 拒绝未知键，是为了让「写错了键名」变成加载失败而不是被忽略。

## 4. 拒绝面（判据的反面必须会红）

`caliber-export-face.txt` 第 3 面是现场调用，不是复述用例：

```
去掉 adjust：RuntimeError -> domain 'stock_daily' in …/probe.yaml does not declare 'adjust'; expected one of ['not_applicable', 'unadjusted']
去掉 suspension：RuntimeError -> … does not declare 'suspension'; expected one of ['absent_row', 'not_applicable', 'unmeasured', 'zero_price_row']
去掉 denominator：RuntimeError -> … does not declare 'denominator'; expected one of ['key_union']
多一个表不认的键：RuntimeError -> … has unknown keys ['adjust_kind']
```

读取缝的拒绝面由用例钉（`pytest-caliber-cases.txt`，23 passed / 46 deselected，
`-m "not e2e" --no-cov` ⇒ 取证跑不是门禁读数）：

* `index_daily` 请求 `qfq` → `RuntimeError … adjust='not_applicable'`；
* 两腿 `suspension` 不一致 → 校对在归一化前就被拒；
* 两侧都 `unmeasured` → 校对被拒且报的是「没有实测停牌形状」；
* 出厂两腿（`absent_row`/`absent_row`/`key_union`）→ 中文列名帧走真表口径一路
  `CONSISTENT`，`compared_keys=2`（证门禁不是把真表也挡住了）。

## 5. 反事实面为什么仍然算数

`AC-9|01` 的 8 条 `Break` 全部 expect `gap`，而本格现在也是 `gap`——看起来像空转。
不是：`Break.facts` 的覆盖是打在 **`repair` 之后的干净读数**上的（`Break` dataclass
docstring：*"Overrides applied to the *repaired* facts, so each break isolates one
face"*；`self_test_findings()` docstring：*"every counterfact flips that clean reading
back to a gap. A break applied to an item that is already red proves nothing, which is
why the measured facts are not the start point"*）。门禁成员 11 每遍都会跑
`--self-test`，两件事都要成立：

1. `repair` 读数能把 judge 打到 `proven`（否则 gap 永远关不掉）；
2. 每条 break 都能把那个干净读数打回 `gap`（否则判据恒红或恒不咬人）。

`AC-9|01` 的 `repair` 是 `p0_mapped: *p0_total` + 六个 `cat_*: yes`，其 hint 行
`补齐到可判：p0_mapped: 3 -> 5; p0_missing: financial_indicator, financial_statement -> -`
就是本轮余下面目：**能扳红判据的是 P0 覆盖，六类口径那六条已经落在可翻转的面上**
（本轮之前 `cat_adjust/cat_suspension/cat_denominator` 实测就是 `no`，那三条 break
与现场读数同色；现在现场读数为 `yes`，它们才第一次成为真正的反事实）。

## 6. C49 仪器复现：签名变了、读数没变（披露）

`opendata/data/mapping.py` 把三类口径变成必填之后，历史仪器里手工构造
`DomainMapping(...)` 的站点会编译不过。`docs/evidence/C49/ac9_residual_census.py`
是上一轮入库的仪器，本轮给它补了三个值（两侧同值：`unadjusted`/`absent_row`/
`key_union`），这不改它测的东西，但**必须能证明没改它测出来的结论**。复跑同一命令，
与 C49 归档读数逐行 diff（`c49-instrument-rerun.txt` 与
`c49-instrument-rerun-vs-c49-archive.diff`）：第 2–8 面测量读数逐字未变；变的只有

* 第 1 面 3 行台账状态（`|03 unreviewed→proven`、`|08 unreviewed→gap`、
  `|09 unreviewed→proven`）与其行号漂移——那是 C50/C52 的 backfill 结果，不是本轮；
* provenance 头 8 行（归档时那遍的手写头）；
* 一行 loguru `INFO` 在 stderr 里的位置（同一句，先后次序不同）。

## 7. 仪器与命令（逐字可复跑）

```bash
# 停牌语义与复权基准实测（只读 SELECT；两条腿同一命令）
python -u scripts/ops/suspension_shape_check.py --symbols 200
# 三条无 ods 行的腿：同一仪器按域复量（预期 1146 表不存在）
for d in index_daily futures_daily option_daily; do
  python -u scripts/ops/suspension_shape_check.py --domain "$d" --sources ths
done
# 口径导出面与加载器拒绝面（不连库）
python -u docs/evidence/C53/caliber_face.py
# 条目级判据读数（判定看 VERDICT 行，exit 只反映措辞漂移）
python -u scripts/quality/acceptance_item_probe.py --item 'AC-9|01'
```

库侧 census（`ods-leg-census.txt` 由这段逐字产生，只读、不打印连接串）：

```python
from sqlalchemy import create_engine, inspect, text

from opendata.core.config import get_settings
from opendata.data.domains import dwd_table, ods_table
from opendata.data.mapping import load_mapping, mapping_sources

engine = create_engine(get_settings().data_database_url)
names = sorted(inspect(engine).get_table_names())
print(f"库内表 {len(names)} 张（全库，精确 COUNT(*)）：")
counts = {}
with engine.connect() as conn:
    for name in names:
        counts[name] = int(
            conn.execute(text(f"SELECT COUNT(*) FROM `{name}`")).scalar_one()  # noqa: S608
        )
for name in names:
    print(f"  {name:<32} rows={counts[name]}")

print()
print("口径映射表声明的每一条腿（domain, source）在库里的落点与声明的三类口径：")
for source in sorted(mapping_sources()):
    mapping = load_mapping(source)
    for domain in sorted(mapping.domains):
        entry = mapping.domains[domain]
        table = ods_table(domain, source)
        exists = table in counts
        print(
            f"  {domain:<20} {source:<8} ods={table:<28} "
            f"exists={'yes' if exists else 'no '} rows={counts.get(table, 0):<9} "
            f"adjust={entry.adjust:<14} suspension={entry.suspension:<15} "
            f"denominator={entry.denominator}"
        )
    print()
print("dwd 目标表：")
for domain in sorted({d for s in mapping_sources() for d in load_mapping(s).domains}):
    table = dwd_table(domain)
    print(f"  {domain:<20} {table:<24} rows={counts.get(table, 0) if table in counts else '<无表>'}")
```

`scripts/ops/suspension_shape_check.py` 是**本轮首次入库**：C52 及之前的轮次把它留在
工作树里没提交（`git log -- scripts/ops/suspension_shape_check.py` 本轮之前为空）。
一份只存在于某台机器上的仪器不能作为判据的证据面，所以随本轮一起 commit。

## 8. 仍未闭合的面（登记）

1. **`AC-9|01` 的覆盖半**：P0 域 3/5，缺 `financial_statement`/`financial_indicator`
   在这张表里的声明。**本轮回填时把这一面写重了**，gate 第 1 遍跑完后读产品事实改口：
   原文写「这两条腿是宽表，进这张表要先给表加『宽→长』结构」——不成立。
   * **不需要 DDL**：`_DWD_TABLES`（`alembic_data/versions/20260923-0001_ods_dwd_p0.py:33-40`）
     的业务键已分别是 `(symbol, statement_type, report_period, item)` 与
     `(symbol, report_period, indicator)`，契约 `FinancialStatement` /
     `FinancialIndicator`（`opendata/data/models/financial.py:10,34`）docstring 第一行
     就是 "One statement line in long format" ⇒ dwd 侧早就是长表。
   * **ths 腿不需要中文条目字典**：`item` 取的就是上游给的英文码，C10 已在 2026-09-25
     实测三张表完整键集并落成 `opendata_fuyao/endpoints.py:93` 的
     `FINANCIAL_STATEMENT_ITEMS`（income 13 / balance 7 / cashflow 6 = 26 个科目，
     段首注释逐条写明「减去共有元数据键」）。这份硬编码清单正是判据点名的
     「口径散在代码里」的那一处——该进表的它是现成的。
   * 真正缺的两样：映射表没有能表达「宽→长 pivot」的形状（`_FIELD_KEYS` 只有
     `from`/`scale`/`normalize`/`ms_column`，是 1:1 改列名，装不下「把这 26 列熔成
     `item`/`value`」），以及 `financial_indicator` 的单位与披露日口径还没实测
     （C10 留下的事实：该端点数值是字符串、且无任何披露日期字段，而契约
     `FinancialIndicator.announce_date` 必填 ⇒ 不猜一个日期凑数）。
   * 中文科目名 ↔ 英文码那份需人工复核的字典确实还缺，但它属于**合并键**那一面
     （sina 搬运腿落中文名、ths 腿落英文码 ⇒ 同一事实写成两行，AC-4 人工归因），
     不是「这个域能不能写进映射表」的前置条件。C54 = 任务 #67 按上面重述的范围做。
2. **差异率分母是被校验、不是被盖章**：`require_comparable_calibers()` 要求两侧都声明
   `key_union` 才放行校对，但 `dq_diff_report` 的每行里没有这一格——
   `opendata/pipeline/diff_report.py:27` 的 `REPORT_COLUMNS` 是固定 11 列元组，把声明值
   写进报告需要一次 DDL + 生产写入，按同一规则留给用户确认后才做。
3. **停牌形状只有一处真读取（关键词面）**：`suspension-single-seam-grep.txt` 面 A 是
   按 `suspension`/`halted`/`停牌` 三个关键词对 `opendata/` 全树的 grep——除
   `data/mapping.py`（枚举、必填、拒绝、导出）与 `pipeline/cross_check.py` 这条缝
   （161-162 行是它的 docstring）之外，产品 `.py` 里没有第二处命中，其余命中全在两份
   yaml 的声明与注释里。这条是关键词面而非语义面：它排除的是「另有一处写着停牌假设」，
   不排除某段代码用别的词隐含了缺行语义；后者留给 AC-9|03/|05 的判定面。
4. **期货/期权停牌待测**：等 `ods_futures_daily_ths`/`ods_option_daily_ths` 真的落库，
   用 §7 的命令复量后把 `unmeasured` 换成实测值；替换前任何双源校对都会被拒绝。
5. **akshare 腿 38 行零量零额**：C24 归因的 sina 退路缺额度面，未写进停牌声明，
   作为数据质量待修登记。
6. `index_constituent` 的 ods 表在本库 exists=no（census 可见），其 `not_applicable`
   是定义性声明而非实测——若该腿日后落库，需要复核这格声明仍然成立。

## 9. 读数清单

| 文件 | 是什么 |
| --- | --- |
| `caliber_face.py` | 本轮仪器（导出面 / 逐字段口径 / 加载器拒绝面） |
| `caliber-export-face.txt` | 上表的读数（7 条腿三类口径 + 4 条拒绝） |
| `caliber-reader-callsites.txt` | 两个读取缝在产品代码里的调用点 grep |
| `suspension-shape-stock-daily.txt` | `stock_daily` 两腿停牌形状与基准实测（第二次复跑，与 23:44 那次数值逐字相同） |
| `suspension-shape-no-ods-legs.txt` | 三条腿无 ods 表（1146 原文 + SQL） |
| `ods-leg-census.txt` | 库内 12 表精确行数 + 每条腿 ods 落点与三类声明 |
| `probe-ac9-01.txt` | 条目级探针读数与 VERDICT（gap，理由只剩 P0 覆盖） |
| `pytest-caliber-cases.txt` | 口径相关用例取证跑（23 passed） |
| `a2-check-changed-files.txt` | 本轮 8 个改动文件的 A2 读数 |
| `c49-instrument-rerun.txt` / `-vs-c49-archive.diff` | §6 的复现与逐行差异 |
| `suspension-single-seam-grep.txt` | §8 第 3 条的关键词面（全树 grep，A/B/C 三面） |
| `gate-run1-finaltree.txt` | `make gate` 第 1 遍完整日志（回填后；17 成员 / 18 段横幅，`MAKE_EXIT=0`，正文未裁剪） |
| `gate-run2-finaltree.txt` | `make gate` 第 2 遍完整日志（§8 更正与仪器入库之后，最终树） |

§8 第 1 条是一次**本轮内的自我更正**：回填时把覆盖半的障碍写成「要给表加宽→长结构」，
gate 第 1 遍跑完后逐条读 `_DWD_TABLES`、契约模型与 `FINANCIAL_STATEMENT_ITEMS` 三处
产品事实，确认那个说法把难度写重了（DDL 不需要、ths 腿的条目字典也不需要），已按事实
改口并把更正理由留在原位——不静默覆盖，因为「当时以为缺一件结构」这件事本身就是
下一轮要避开的坑。台账与验收文档里的同一句也随之改。

## 10. 并发与只读纪律

库侧三条命令全程 `SELECT`（census 另加 `information_schema`/inspector 反射读），
无 DDL、无写入、不打印连接串；`.env` 只 `set -a; . ./.env; set +a` 注入，未读取内容。
本轮没有临时容器化：`caliber_face.py` 的探针 yaml 写在 `TemporaryDirectory` 里，
脚本返回即删；`FUYAO_API_KEY`/`FRED_API_KEY` 未被读取或打印。门禁两次跑（回填前后）
期间不再并发跑任何 pytest/探针，`docs/evidence/` 在探针遍进行中不写入；档案里的
门禁日志是完整未裁剪正文 + 溯源头，`GATE_EXIT` 读自档案自身。
