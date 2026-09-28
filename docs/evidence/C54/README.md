# C54：宽→长的科目口径成为口径映射表的域级 `pivot:` 字段并被三个熔解器读取（AC-9\|01 的「覆盖」半翻正，本格转 proven）

判据原文（§2 L178；本轮在变更日志插入 v5.26 一行后该条目位于 L179，判据文字一字未动）：
**口径映射表**存在且覆盖 P0 域（字段映射/单位换算/复权口径/key 规范化/停牌语义/差异率分母）。

C53 把这格量成 gap，读数只剩覆盖半：P0 域 3/5，`financial_statement`/`financial_indicator`
两条腿不在这张表里。本轮把这两条腿做实——不是把域名抄进 yaml，而是给表加一个能表达
「宽帧怎么熔成长表行」的域级形状 `pivot:`，让三个熔解器改读它、把各自硬编码的科目/元数据
名单删掉。**这条界线是本轮的全部意义**：判据的覆盖面按 yaml 里有没有这个域来读，所以抄名字
也能把 `P0 覆盖` 扳到 5/5；那叫把判据做算术。要让它不是算术，声明必须是有读者的声明——
改表就改熔解结果（§5 的三组反事实），不声明就熔不出行（§4 的拒绝面）。

探针读数（前：`docs/evidence/C53/probe-ac9-01.txt`；后：`probe-ac9-01-postflip.txt`，逐字）：

```
- 六类口径落表: 字段映射=yes、单位换算=yes、复权口径=yes、key 规范化=yes、停牌语义=yes、差异率分母=yes
- P0 覆盖: 3/5 个 P0 域在这张表里；没覆盖的是 financial_indicator, financial_statement
VERDICT AC-9|01: gap          →   - P0 覆盖: 5/5 个 P0 域在这张表里
                                 VERDICT AC-9|01: proven
```

判据原文一字未动：`wording_drift` 的 `expects` 仍是 §2 那一行的逐字子串；探针源码里 AC-9\|01 的
`judge`、`repair`、8 条 `Break` **一个都没改**（本轮没有把判据改窄，也没有把判据改宽——pivot 不是
判据点名的第六类口径之一，所以它不进 judge，只作为「两个域能不能被真实读出来」的证据面）。

---

## 1. 改动面（谁声明、谁读、谁拒绝）

| 面 | 文件 | 本轮变成什么 |
| --- | --- | --- |
| 声明结构 | `opendata/data/mapping.py`（+502 行） | 新增 `PivotMapping(mode, item_field, value_field, group_field, groups, excluded, row_columns)` 与三个读法 `item_columns(group)` / `line_items(columns)` / `row_column(contract_field)`；`DomainMapping.pivot`（默认 None）与 `_DOMAIN_KEYS` 的 `pivot` 键；加载器侧 18 个 `raise` 站点（`_parse_pivot` 5 / `_parse_pivot_rows` 4 / `_require_pivot_group_field` 1 / `_parse_pivot_groups` 6 / `_parse_pivot_passthrough` 2，按 AST 计数而非印象）；`require_pivot(source, domain)` 是唯一入口（它自己另 1 个站点）；`_pivot_as_json` 随 `mapping_as_json` 导出，1:1 域导出 `None` 而不是空声明 |
| 表内容 | `opendata/data/mappings/ths.yaml`（+74）、`akshare.yaml`（+69） | 三个 financial 域各写一个 `pivot:` 块（ths 报表=groups 13/7/6、sina 报表=passthrough+5 列页头、em 指标=passthrough+SECUCODE），每行上方注明取值来源；同一域补 `fields:` 长视图与三类口径声明 |
| 读取缝 1 | `opendata_fuyao/endpoints.py` | **删除 `FINANCIAL_STATEMENT_ITEMS`（26 个英文科目码的硬编码）**，改为 `financial_statement_items(statement_type)` 读表；`normalize_financial_statements()` 的披露列改读 `row_column("announce_date")`；表里没有这组科目就抛 `FuyaoError(envelope_invalid, detail="financial_statement_type")` |
| 读取缝 2 | `opendata/data/providers/akshare/models/financial_statement.py` | 删 `_METADATA_COLUMNS`；科目全集改 `require_pivot(SOURCE,"financial_statement").line_items(raw.columns)`，报告期末/披露日改 `row_column(...)`。source 名读同包已有的 `SOURCE`（`_source.py`：路由标签按目录名派生），不写字面量——理由写在那个文件里，见 §7 末 |
| 读取缝 3 | `.../models/financial_indicator.py` | 删 `_DIMENSION_COLUMNS`；取数前的披露日列名、熔解时的科目全集与两个行级列都来自表（同一 `SOURCE`）；缺列即 `ValueError` 并点名表里登记的那一列 |
| 用例 | `tests/test_mapping_pivot.py`（新增 42 例：出厂声明 6 / 读取面 9 / 解析守卫 19 / 「改表就改熔解」5 / 导出面 3） | 判据的每一个「会坏」面各有一条用例；科目词汇用字面量钉死（13/7/6 + 首两个 + 末个 + 三组互斥），不靠读表自证 |
| 用例（连带） | `tests/test_fuyao_endpoints.py`、`tests/test_ths_provider.py` | 把 `FINANCIAL_STATEMENT_ITEMS[...]` 换成 `financial_statement_items(...)`。这两处**只测熔解行为**（宽行→长行、条数=科目数×报告期数、缺键 vs null 的分别），科目词汇由表供给 ⇒ 对词汇本身是自指的，所以词汇的非自指见证在上面那行字面量用例里，见 §6 末 |
| 用例（连带） | `tests/test_alert_matrix.py` | 「读不到的腿是多数」那一条的重钉：26 → 23（三条 financial 腿现在能读 `report_period`），同时把方向也钉住（`for leg in (...)` 三条显式不在 unmapped 里），否则这个数字可以往任何一边漂。这不是放宽：是被本轮改动**正当要求**的读数迁移 |
| 仪器 | `docs/evidence/C54/caliber_pivot_face.py`（首次入库） | A–G 七面，见 §7 |

## 2. `pivot:` 的两种模式与每个取值的证据

模式的选择不是审美：**科目词汇有没有一份可登记的闭集**决定它。

| 腿 | mode | 为什么是这个模式 | 取值证据 |
| --- | --- | --- | --- |
| `ths.financial_statement` | `groups` | fuyao 的 `data.item[]` 每行是一张表的全部科目，2026-09-25 实测过完整键集 ⇒ 词汇是闭集，可以让表说了算；用 `passthrough` 就等于把「响应这遍多给了什么列」当成科目定义 | `groups` 13/7/6 = 26 科（`docs/evidence/C10` 实测键集减去共有元数据键 `thscode`/`ticker`/`period`/`fiscal_year`/`fiscal_period`/`report_date_ms`/`period_end_ms`/`currency`，裁剪过程写在 yaml 段首注释）；`row_columns.announce_date = report_date_ms`（实测与新浪「公告日期」逐期一致，含追溯调整错位） |
| `akshare.financial_statement`（sina 搬运腿） | `passthrough` | sina 一页给一张表、列就是中文科目名，**没有**一份人工复核过的科目闭集；这时能诚实登记的只有「哪些列不是科目」 | `excluded = [数据源, 是否审计, 币种, 类型, 更新日期]`（实测页头元数据列）；`row_columns = {report_period: 报告日, announce_date: 公告日期}`（帧里真实列名） |
| `akshare.financial_indicator`（em 腿） | `passthrough` | 同上：em F10 的列是上游字段码（`EPSJB`/`ROEJQ`…），契约 `indicator` 就是「把源码带到归一码落地之前」，没有闭集可登记 | `excluded = [SECUCODE]`（页内标的列，身份由请求参数给）；`row_columns = {report_period: REPORT_DATE, announce_date: NOTICE_DATE}`——`NOTICE_DATE` 是 C53 登记的那个缺口（sina 指标页没有任何披露日期字段，em 数据集有），契约 `announce_date` 必填因此靠它成立，缺列即 `ValueError` 失败关闭 |

三类口径声明在两条 financial 腿上都是 `adjust: not_applicable`（报表数没有价格序列，查询层据此
拒绝 qfq/hfq）、`suspension: unmeasured`、`denominator: key_union`。`unmeasured` 的分工与 C53 同一条
规矩（不猜一个填上，并给占位符一个会拒绝的读者）：报表缺行是「那一期没有披露」而不是标的停牌，
且 sina 腿落中文科目名、ths 腿落英文码，两腿行形状不同 ⇒ 无从实测；`require_comparable_calibers()`
现在拒的就是这对组合，用例 `test_the_two_financial_legs_cannot_be_compared_yet` 钉住
`match="no measured suspension shape"`（仪器面 C 的两侧同值 `unmeasured` 也走同一条拒绝）。

`unit` 与 `scale` 为什么在这两条腿是空的：em 数据集逐列混着元、元/股与 %（C10 实测过它是字符串、
本轮实测过它没有单位列），**没有实测过逐列口径就不声明单位**——契约 `FinancialIndicator.unit` 留
`None`。这不是「省略了默认值」，表里 `fields.unit` 只有 `{from: unit}`（长视图的名字对应），
`scale` 一格都没写。判据的「单位换算」这一格在 financial 两腿因此是**结构上可承载、内容上待测**，
§8 第 3 条登记。

## 3. 六类口径现在分别落在哪里（financial 两腿的视角）

| 口径 | 落点 | 读数证据 |
| --- | --- | --- |
| 字段映射 | `fields.<name>.from`（长视图）+ **`pivot.row_columns`**（宽→长的那一层） | `caliber-pivot-faces.txt` 面 B、面 F |
| 单位换算 | `fields.<name>.scale`；financial 两腿无实测 ⇒ 一格不写 | 面 B（`excluded`/`row_columns` 逐字段导出）+ §2 末段 |
| 复权口径 | 域级 `adjust` → `require_adjust_basis()` | C53 面；本轮两腿为 `not_applicable`，请求复权即拒 |
| key 规范化 | `fields.symbol.normalize: plain` + 域级 `key` | 面 B（`key=['symbol','statement_type','report_period','item']`、`key=['symbol','report_period','indicator']`） |
| 停牌语义 | 域级 `suspension` → `require_comparable_calibers()` | 面 B + `test_the_two_financial_legs_cannot_be_compared_yet` |
| 差异率分母 | 域级 `denominator: key_union` → 校对层读 | 面 B；**这一格在 financial 两腿今天不被执行**（停牌先拒），见 §8 第 4 条 |

「科目全集」不是判据点名的六类之一，所以它**没有**进 judge；它是「字段映射」这一类在宽表域里的
真实形状：`normalize_frame` 逐列投影，宽帧里没有一列叫 `item`，所以 1:1 的表结构**表达不出**这一域
——这正是 C53 §8 第 1 条登记的那件缺的结构。

## 4. 拒绝面（判据的反面必须会红）

`caliber-pivot-faces.txt` 面 D 是现场调用原文（不是复述用例）：

```
1:1 域问 pivot : RuntimeError: domain 'stock_daily' of source 'ths' declares no pivot block; its columns are 1:1 with the contract, so there is no wide frame to melt (fail closed)
未登记的报表组 : RuntimeError: pivot declares no group 'shadow'; known groups: ['balance', 'cashflow', 'income'] (fail closed: an undeclared group has no item vocabulary)
groups 模式问 line_items : RuntimeError: pivot mode 'groups' needs item_columns(group); the frame's own keys do not decide what a科目 is (fail closed)
未登记的行级列 : RuntimeError: pivot declares no row column for 'currency'; declared: ['announce_date', 'report_period'] (fail closed)
宽帧走 1:1 投影 : ValueError: source frame is missing mapped columns ['announce_date', 'item', 'report_period', 'revision', 'statement_type', 'symbol', 'value'] for domain 'financial_statement'; …
```

加载器侧 18 个 `raise` 站点被 `TestPivotParsingGuards` 的 19 例逐个扳红（例比站点多一条，是因为
`_require_pivot_group_field` 那一处从两个入口各测到一次：一是 `groups` 模式没写 `group_field`，
二是把 `group_field` 写成非业务键字段）。逐条形状：未知键、
未知 mode、`value_field` 指向未声明字段、item 与 value 同一字段、`groups` 与 `excluded` 同时出现
（允许清单与拒绝清单不能同时拥有科目集）、`passthrough` 下写 `groups`、缺 `excluded`、`groups` 模式
缺组、空组、组内重复科目、同一名字既是科目又是行级列、`group_field` 不是业务键字段（写成
`announce_date` 即拒）、`row_columns` 不是映射、行级字段在域里没有 mapping、`row_columns` 列名为空、
把 produced field 当列读回来（`item` 从列读——熔解本身就产出它）、整块写成字符串。

## 5. 反事实面：改表就改熔解结果（这一面把「声明」和「装饰」分开）

面 E 三组，全部是现场调用，写在 `TemporaryDirectory` 里的探针表上，脚本返回即删：

1. **数值型「更新日期」**：出厂表把它列在 `excluded`，熔解出 `item = ['应收账款', '货币资金']`；
   从 `excluded` 里去掉这一行声明，同一帧熔出 `item = ['应收账款', '更新日期', '货币资金']`，
   `差集 = ['更新日期']`——表少一行声明，熔解就多一个科目。
2. **行级列名**：把 `row_columns.report_period` 改成一个不存在的列名 ⇒ `行数 = 0`
   （读不到报告期末就不出行）。这一组的读法要谨慎：0 行不是「失败关闭的报错」而是
   `continue` 掉的自然结果，本轮就按 0 行报告，不把它写成拒绝面。
3. **ths 的 `income` 组**：把表里那一组改成只剩 `only_this_item` ⇒
   `financial_statement_items("income")` 返回 `('only_this_item',)`——fuyao 的科目词汇跟着表走。

第 1 组值得单独记一句**取证过程**：第一版面 E 用的是日期字符串 `更新日期`，改 `excluded` 前后
输出都不动——因为挡住它的是数值强转，不是声明。这会让这一组成为一个不咬人的反事实，所以换成
数值型 `20240830` 的夹具让它真的咬；没咬动的那一半没有删掉，而是升成了独立的**面 G**（§6）。

## 6. 声明了但今天不改变输出的格（自我披露，不主张「今天有用」）

判据翻正之后最容易说过头的话是「每一个字节的声明都有读者」。本轮的读数是三个反例，逐条登记：

* **面 G 第 1 组**：把 sina 腿的 `excluded` 整块清空，熔解结果**不变**（仍 `['货币资金', '应收账款']`）。
  挡住这些字符串型元数据列的是 `_numeric()` 强转，`excluded` 的行为差别只在元数据列本身可转成数时
  出现（面 E 第 1 组）。⇒ 这一格按「口径要先声明、待测口径不许猜」登记，**不按**「今天它改变输出」主张。
* **面 G 第 2 组**：`group_field` 的读者只有加载器（组必须落在业务键字段上）与导出面；三个熔解器
  都不读它——组值来自请求参数，不来自响应列。
* **面 F**：本库**没有** `ods_financial_statement_ths` / `ods_financial_statement_akshare` /
  `ods_financial_indicator_akshare` 任何一张表（A4.1 迁移只建了日线四件套与目录，`dwd_financial_*`
  两表 0 行）。⇒ 两条 financial 腿的 `fields:` 写的**不是某张 ods 表的列名拼写**，而是摊平后的长表
  视图；它今天有读者的那一格是 `report_period`（巡检/告警的新鲜度字段面，正是 `alert_matrix` 那
  26 → 23 的来源），`revision`/`unit` 两格是名字对应、无转换、无实测来源（`revision ≡ 1` 是逐行常量、
  `unit` 恒 `None`，两者都不是「列名映射」，表里放不进逐行常量——§8 第 2 条）。
* **词汇见证的非自指性**：`tests/test_fuyao_endpoints.py` 与 `tests/test_ths_provider.py` 现在从表里
  取科目词汇（`financial_statement_items(...)`），对词汇本身是自指的（表说什么它们就 expect 什么）。
  非自指的见证在 `TestShippedMeltDeclarations::test_the_grouped_item_vocabulary_is_pinned`：13/7/6 三个
  字面量计数 + `('operating_income', 'operating_costs')` 首两个 + `basic_eps` 末个 + 三组互斥，
  来源注明 2026-09-25 的实测。这一条写在档案里，是因为「42 例全过」这个数字本身分辨不出这两种见证。

## 7. 仪器与命令（逐字可复跑）

```bash
# 七面读数（不连库；只读 yaml + 注册 provider 表，探针表写在 TemporaryDirectory 里）
python -u docs/evidence/C54/caliber_pivot_face.py
# 条目级判据读数（判定看 VERDICT 行，exit 只反映措辞漂移）
python -u scripts/quality/acceptance_item_probe.py --item 'AC-9|01'
# 口径用例取证跑（--no-cov 且 -m "not e2e" ⇒ 不是门禁读数）
python -m pytest tests/test_mapping_pivot.py -q --no-header -p no:cacheprovider --no-cov -m "not e2e"
python -m pytest tests/test_mapping_pivot.py tests/test_data_mapping.py tests/test_p0_providers.py \
  tests/test_fuyao_endpoints.py tests/test_ths_provider.py tests/test_alert_matrix.py \
  -q --no-header -p no:cacheprovider --no-cov -m "not e2e"
# 关键词面（哪些标识符已经消失 / 谁在读 pivot）
grep -rnE "(^|[^A-Za-z_])FINANCIAL_STATEMENT_ITEMS" --include='*.py' opendata/ opendata_fuyao/ scripts/ tests/
grep -rn --include='*.py' -E "require_pivot|\.line_items\(|\.item_columns\(|\.row_column\(" opendata/ opendata_fuyao/
# 零依赖门禁面（本轮新增调用点必须不把上游根名写成字符串常量）
python -u scripts/codemod/verify_no_akshare.py
```

`hardcoded-items-grep-and-readers.txt` 里记了一条**取证纪律上的自我更正**：第一版关键词命令用了裸
子串 `_METADATA_COLUMNS`，被 `opendata/pipeline/ddl.py` 的 `ODS_METADATA_COLUMNS`（ods 审计列，与本轮
无关）以子串命中 4 次。改成前置非标识符字符的窄正则才读到 `hits=0`；裸子串那遍的 `hits=4` 原文
留在档案里，读者可以核对差别只在模式。若当时直接把 `hits=0` 写进档案，那将是一次被模式宽窄救回来的
误读——它不是本轮的证据，是本轮的一个坑。

第二处自我更正，性质与上一条不同：**它是门禁自己红的，不是我读错**。第一版的三个新调用点写成
`require_pivot("akshare", …)`，`make gate` 成员 2（`zero-dep-check`）当场 `FAIL: 3 new upstream
reference(s)`（indicator `[string] akshare x2`、statement `x1`），`make` 就在成员 2 停下（所以那一遍
只走到第 2 个成员，后面的成员那一遍没执行——这一格按「门禁在早期成员就拦住」记录，不按「全表跑过」
主张）。改法是**照本包既有的约定**：路由标签读 `opendata/data/providers/akshare/_source.py` 的
`SOURCE`（该文件把「为什么不能用字面量」写在 docstring 里），门禁判据与扫描面一字未动。改后
`verify_no_akshare.py` 回到 `OK: 517 file(s) walked … no new upstream references (frozen baseline: 3)`，
仪器第四次跑的 body 与第三次逐字节相同（熔解读数不受影响），`tests/test_mapping_pivot.py` 42 例与
六文件 330 例仍全绿，`--item 'AC-9|01'` 仍 `VERDICT … proven`。第一版那三条命中的可复现读数
（扫的是索引里的旧 blob，命令写在档头）见 `zerodep-red-first-pass.txt`。

⇒ 这一格值得留着的理由：AC-9\|01 翻正靠的是「把科目词汇从代码搬进表」，而搬的过程中在代码里
新写了一个上游根名字面量——同一个改动自己引入了它邻域的反例。门禁在成员 2 就拦住了它，
没有靠 reviewer 的眼睛。

第三处红得更远一层：**这一遍红的不是本轮写的代码，而是本轮把它推进可见面的历史债**。`make gate`
第一遍走到成员 12 停下（`GATE_EXIT=2`，正文只有 12 段 ⇒ 后面的 frontend 各段没被这一遍执行）。红的是
C45 那条守卫用例 `tests/test_a2_exclusion_scope.py::test_mypy_walks_both_migration_envs`：它断言
`mypy alembic alembic_data` 退出 0，而 `alembic/env.py:13` 为了 autogenerate 收集模型写着
`import opendata.models` ⇒ 这一平面把 `opendata/models/{user,interface,task,data_table,data_script}.py`
读成 5 个 `Module "datetime" has no attribute "UTC"`：这五个文件用的是 3.11 才有的 `UTC` 别名，
而项目声明的目标是 `[tool.mypy] python_version = "3.10"`。这一格值得留的不是"修好了"，而是
**同一条命令在门禁之外单跑读到 `Success`**（同仓库、同解释器、同 `pyproject.toml`）：这个断言的绿
依赖 import 解析状态，所以它不是偶发红，是偶发绿。两种处置里选了消掉真实错误——改用 `timezone.utc`
（实测 `timezone.utc is UTC` 为真 ⇒ 零行为变化，不动模型语义），并按「触碰即达标」把五个文件自带的
23 条 ruff 一并清掉（13 条 D212、8 个 `__repr__` 文档串、2 处超长行）；没有改窄那条断言，没有改
`python_version`，没有加 `# type: ignore`。修后：alembic 平面 `Success: no issues found in 10 source files`、
`mypy opendata/models/` 0 错、`bandit` 0 issue、非 e2e 全表 3342 passed / 6 skipped、
`mypy_selfdev` 11→6 与 `ruff_selfdev` 242→219（棘轮已冻结）。

第四处是**我自己造出来的假绿**，与本轮代码无关但性质最重：给那 6 个交还的文件量测"需要判断的那几条"时，
我写成 `python -m ruff check --select S105,S106,SIM114,F401 $SIX 2>/dev/null`，而 zsh 不对未加引号的变量
做词分割 ⇒ 六个路径变成一个不存在的长路径；限定 `--select` 之后 ruff 把 `No such file or directory`
降级成 stderr 上的 warning，stdout 照打 `All checks passed!`、退出码 0，`2>/dev/null` 又把唯一的信号删了。
一份**不存在的绿色**就这样读出来了。同一对命令的原文（不存在路径在全规则集下红、在 `--select` 下绿、
真实路径下红）逐条留在 `datetime-utc-debt-fix.txt` 末段。采用的规矩：量测不得吞 stderr；限定 `--select`
时必须另跑一遍不带 `--select` 的确认路径存在；条数以工具自己的 `--statistics` 为准，不靠行数相减
（本档案先前那版就因把页脚行算进每文件条数而多报了 2 条，已就地更正，更正后的总数 69 由 ruff 自数）。
登记为工具面缺口：`ruff check --select …` 对缺失路径 exit=0，与本库已登记的 `git ls-files -z`、
`--strict-markers` 只管一个方向那两条同类。

## 8. 仍未闭合的面（登记）

1. **`financial_indicator` 只有 akshare 一条腿**：ths 没有注册这个域，`opendata_fuyao` 也没有对应
   端点面（面 A 的 `（ths 没有注册 financial_indicator 腿，表里也没有——不是漏声明）` 就是这一条）。
   ⇒ 判据说的「覆盖 P0 域」在这一域上是单腿覆盖，跨源校对在这一域今天无从发生；补 ths 腿要先有端点，
   不是本轮能靠声明补的。
2. **两个域里各有一格口径进不了这张表**：`report_period` 在 ths 腿是 `fiscal_year + fiscal_period`
   推出来再与 `period_end_ms` 对账的（两列合成一个字段，1:1 与 pivot 都表达不了，留在
   `_financial_report_period()`）；`revision ≡ 1` 是逐行常量而不是列名映射。⇒ 这两格是**表结构的面**，
   与 C53 登记 `dq_diff_report` 分母盖章同一类：要补的是形状，不是声明。
3. **`financial_indicator` 的单位待测**：`unit` 恒 `None`、`scale` 一格不写（§2 末段）。要填这一格需要
   逐列单位实测（em 数据集里没有任何单位字段，得靠人工复核字段码字典），本轮不猜。
4. **`denominator: key_union` 在 financial 两腿今天不被执行**：停牌语义先拒了比对（`unmeasured`），
   所以这一格是「声明了、等待两腿形状统一之后才会被读到」。写在表里是为了让分母不再由实现决定，
   但本轮不主张它有任何当前行为。
5. **两腿的 `item` 合并键仍未统一**：sina 腿落中文科目名、ths 腿落英文码 ⇒ 同一事实写成两行，
   中文↔英文那份需人工复核的字典仍然缺（C53 已把这格定性为合并键面而不是覆盖面，本轮维持）。
   `statement_type` 的取值集合也只在 `groups` 模式下由表管（ths 腿）；sina 腿是 `passthrough`，
   `group_field` 只登记它落在哪个字段上、不登记合法取值 ⇒ `statement_type: str = "资产负债表"` 仍是
   provider 查询模型的默认值加自由串。
6. **无 ods 表 ⇒ 没有真机面**：本轮没有任何库写入，也没有一条 `ods_financial_*` 行可验
   （面 F）。落库要先建表（A4.1 迁移之外的一次 DDL），按既有规则留给用户确认。

7. **同类债还剩 6 个文件、本轮原样交还 HEAD**：`from datetime import UTC` 在
   `opendata/api/{tasks,auth}.py` 与 `opendata/services/{execution,notification,retry}_service.py`
   共 5 处，加 `services/scheduler_service.py:195` 一处未标注参数。它们不在门禁的任何可见平面上
   （alembic 平面只 `import opendata.models`），而把它们清干净要一并吃下 69 条 ruff（`--statistics`
   自数：D415 31 / D212 27 / RUF100 3 / D107 2 / ANN001·E501·F401·S105·S106·SIM114 各 1），其中
   4 条需要判断且落在认证路径（`api/auth.py:7` F401、`:40` S105、`:172` SIM114、`:209` S106）。
   ⇒ 本轮不主张这 6 处已被修好：在一个主张「口径映射表」的轮次里顺手改认证路径与默认口令语义，
   是把两件事的可见面绑在一起。棘轮现冻结在 `mypy_selfdev: 6`，清完可降到 0。

## 9. 读数清单

| 文件 | 是什么 |
| --- | --- |
| `caliber_pivot_face.py` | 本轮仪器（A–G 七面，含三组反事实与两格自我披露） |
| `caliber-pivot-faces.txt` | 上表读数（本机第四次跑；body 与第三次逐字节相同 ⇒ SOURCE 修法不改熔解读数） |
| `probe-ac9-01-postflip.txt` | 条目级探针读数与 VERDICT（回填前一遍，`P0 覆盖: 5/5`、`proven`） |
| `pytest-pivot-cases.txt` | 口径相关用例取证跑（330 passed / 14 deselected；`test_mapping_pivot.py` 单跑 42 passed） |
| `hardcoded-items-grep-and-readers.txt` | 关键词面：已消失的硬编码名单 + `pivot` 的三个读取点 + 模式宽窄那一条披露 + 零依赖那一条披露 |
| `zerodep-red-first-pass.txt` | 门禁成员 2 在本轮第一版代码上的红读数（3 条 `[string] akshare`，逐条 file:line，从索引 blob 重扫得到，可复现） |
| `a2-check-changed-files.txt` | 本轮 9 个改动文件的 A2 读数（含首遍 I001 红与修后复验） |
| `gate-run1-red-alembic-mypy.txt` | `make gate` 第一遍完整未裁剪日志：`GATE_EXIT=2`、正文 12 段（成员 13–17 未被这一遍执行，其读数不由本档案建立）、`1 failed, 3341 passed, 6 skipped`，红在成员 12 的历史 A1 债 |
| `datetime-utc-debt-fix.txt` | 修后逐条读数：alembic 平面 Success、`models/` 0 错、全树残面 6 处、五文件 ruff/bandit 触碰即达标、棘轮 11→6 与 242→219、A2 全集合 385 文件四 ok、`timezone.utc is UTC` 的零行为变化证明，末段是残面清单与那条 exit=0 假绿的原文 |
| `gate-run2-finaltree.txt` | `make gate` 第二遍（最终树）完整未裁剪日志：**18 段成员横幅（满编）**、页脚 `===== gate: PASSED =====`、末行 `GATE_EXIT=0`；成员 11 含全表 `--self-test`，`VERDICT AC-9\|01: proven`、对账行 `agrees=35, unflipped=0, open=0, deferred=1, stale-proof=0`（那 8 条 break 都在**已经 proven 的干净读数**上被扳回 gap）；成员 12 由 run 1 的 FAIL 转 `3342 passed, 6 skipped in 174.22s`、`TOTAL 11805 1085 2956 288 89.47%`；成员 16 前端 109 例、成员 17 前端 e2e 18 例 |
| `post-archive-member-reruns.txt` | run 2 之后唯一的树变更（把上表档案入库）之后的三个成员后置复跑：`ledger-check`、`brand-check`、`evidence-traceability`（585 档案 / 105 条 gate 日志 / 六类 gap 全 0），三条退出码由 subprocess 直跑取得；末段登记两个取证坑（zsh 的 `${PIPESTATUS[0]}` 取空会把 `tail` 的退出码写成成员的、` \| tail -3` 会裁掉 brand 的 12 行 token 面） |

## 10. 并发与只读纪律

本轮**没有任何库操作**：判据面（`probe-ac9-01-postflip.txt`）与仪器（面 A–G）全程只读
`opendata/data/mappings/*.yaml` 与 `alembic_data/` 的迁移源码（AST 直读 `_DWD_TABLES` 的分母，
不连库）；`.env` 未被读取或注入，`FUYAO_API_KEY`/`FRED_API_KEY` 未被读取或打印，无 DDL、无写入。
反事实写的三份探针表都在 `TemporaryDirectory` 里，脚本返回即删。门禁两遍各自进行中没有并发跑任何
pytest/探针，`docs/evidence/` 在探针遍进行中不写入；`gate-run2-finaltree.txt` 是在 `GATE_EXIT=0`
写完之后才组装的，所以它不参与那一遍的任何一个成员；档案里的门禁日志是完整未裁剪正文 + 溯源头，
退出码读自档案自身。`docs/evidence/C54/` 在门禁的 ledger-check / evidence-traceability
两个成员之前已 `git add`——台账引用的证据文件必须被 git 见过，否则「只存在于某台机器上的证据」
在这两个成员里就是零（C53 同条规矩）。
