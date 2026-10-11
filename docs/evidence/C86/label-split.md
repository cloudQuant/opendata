# C86 标签分类拆分：把「今日可声明」的假读数 revert 回可复算的面

## 这一节存在的原因

`LABEL_TO_CAPABILITY` 把 7 个原始标签折叠到已经出厂的能力上，其中两处折叠直接制造了「这 6 行今天就能声明」的读数：
`param_value_mapping` 与 `symbol_normalization` 都被折到 `columns.rename`（registry 判 `present`），
于是它们的行在阶梯里被算成「需求集已被出厂能力覆盖」。拆分把 7 个标签重新指向它们各自真正需要的能力，
并给其中 5 个新能力各加一条 registry 条目（`claim=False, engine_work=True`，锚点钉在**证明该限制存在**的接缝上）。

本节不复述结论，只发布三件事：拆分前后两侧的面、revert 每一处折叠时到底动了哪个计数、以及两处**只动归属不动阶梯**的折叠。

## 载体

| 文件 | 角色 | sha256[:16] |
| --- | --- | --- |
| `docs/evidence/C86/label-split-probe.py` | revert 仪器（读 git 里的拆分前标签表，逐折叠重跑本仓判据） | 见下方复算命令的自摘要 |
| `docs/evidence/C86/label-map-before-split.json` | 拆分前的 96 个标签拼写，取自 `912f59b:scripts/quality/model_capability_census.py` | `ceff278c446a39c7` |
| `docs/evidence/C86/label-split-probe.txt` | 本轮 `--probe` 全文（26 行，逐字） | 见 `wc -l` 面 |
| `docs/evidence/C86/registry-status-both-faces.py` | 两侧 registry 状态分组仪器（把拆分前的表当模块装载并跑它自己的 `run()`） | — |
| `docs/evidence/C86/registry-status-both-faces.txt` | 该仪器本轮全文，末行 `rc=0` 且两侧闭合为 true | — |
| `docs/evidence/C86/roadmap-check-c86.txt` | 拆分后 `--round-id C86 --check` 全文 480 行，末行 `--check verdict: PASS` | — |
| `docs/迭代计划/…/capability-roadmap.json` | 45-canonical 面上重发布的阶梯 | `898e3161b27d2aaf` |
| `docs/迭代计划/…/census-audit.json` | 同代刷新后的审计记录 | `58bb0e08efdcd3a1` |

## 复算命令（三条，按顺序）

```sh
git show 912f59b:scripts/quality/model_capability_census.py > /tmp/c86_pre_split.py
shasum -a 256 /tmp/c86_pre_split.py | cut -c1-16   # fe5a3a55ef600b7f，与载体里 pin 的值同一
python docs/evidence/C86/label-split-probe.py --emit-carrier /tmp/c86_pre_split.py
python docs/evidence/C86/label-split-probe.py --probe     # SPLIT_PROBE folds=7 failures=0，rc=0
python docs/evidence/C86/census-merge-audit.py --self-test && python docs/evidence/C86/census-merge-audit.py --write
```

`label-map-before-split.json` 不是手抄的：它由 `--emit-carrier` 用 AST 从 `git show` 出来的字节里取出
`LABEL_TO_CAPABILITY` 字面量，`pre_split_sha256_16=fe5a3a55ef600b7f` 与上面对 `git show` 输出直接求的摘要同值，
所以「拆分前那张表是什么」这条事实的载体是 git 对象，不是我的记忆。

## 仪器在写之前先撞出的两件事（都留在仪器里当臂，不当 bug 抹掉）

1. **单折叠不是 revert。** 只把 `caller_url_template` 折回 `flow.url_template` 而**不**同时摘掉拆分新增的
   `request.caller_url` registry 条目，本仓判据 `mcc._check_registry` 立刻硬停：
   `RoadmapError: registry names capabilities no census label maps to: ['request.caller_url']`。
   这条守卫是双向的（`missing` 与 `extra` 两个方向），GUARD 臂因此两侧都跑：
   保留条目 ⇒ 被拒；摘掉条目 ⇒ 跑通且 `canonical_capabilities 45->44`。
   我第一次「修」这个报错时把折叠目标写成了 `live_map[label]`（标签**当下**的值），那是一次空折叠——
   它没让 `request.caller_url` 从 canonical 集合里消失，于是守卫换了个方向报
   `registry has no entry for canonical capabilities: ['request.caller_url']`。
   我自己的错修法被守卫的另一侧抓到，这是那条报应面，不是数据面。
2. **不是每处折叠都动阶梯。** 第一版把判据写成「每处折叠必须动四个阶梯计数之一」，`branching_base_url` 立刻
   `nothing moved` 红。真实原因是该折叠把一个 absent 能力换成另一个 absent 能力：行的需求集变了，
   阶梯读数一点没变。于是第五个 pin 的计数改成「该折叠重写了多少行的需求集」，让纯归属类折叠也有面可读，
   而不是让它冒充一次测量。

## 逐臂实测面（`--probe`，rc=0，`folds=7 unique_holder_labels=5 counters=5 failures=0`）

live 面：`{canonical_capabilities: 45, greedy_steps: 32, rows_expressible_today: 3, shipped_cover_rows: 3}`，
`shipped_cover` 三行逐 id 为 `cboe-EquitySearch`、`intrinio-CurrencyPairs`、`tmx-EtfInfo`。
7 处折叠由载体与 live 表逐键比对得出，不是这里写的：
`branching_base_url→flow.url_template`、`caller_url_template→flow.url_template`、
`hardcoded_symbol_roster→columns.rename`、`param_value_mapping→columns.rename`、
`preset_filter_merge→rows.filter`、`sentinel_value_nulling→rows.filter`、`symbol_normalization→columns.rename`。
拆分新增的 5 条 registry 条目同样由折叠表推出（谁的 canonical 只有它一个标签持有），不是手列：
`columns.null_sentinel`、`param.preset_merge`、`param.value_mapping`、`param.value_transform`、`request.caller_url`。

| 折叠 | canonical | expressible | shipped_cover | greedy_steps | 需求集被改写的行数 |
| --- | --- | --- | --- | --- | --- |
| `branching_base_url` | 45 | 3 | 3 | 32 | 4 |
| `caller_url_template` | 45→44 | 3 | 3 | 32→31 | 3 |
| `hardcoded_symbol_roster` | 45 | 3 | 3 | 32 | 1 |
| `param_value_mapping` | 45→44 | 3 | **3→5** | 32→31 | 17 |
| `preset_filter_merge` | 45→44 | 3 | 3 | 32 | 1 |
| `sentinel_value_nulling` | 45→44 | 3 | 3 | 32→31 | 16 |
| `symbol_normalization` | 45→44 | 3 | **3→4** | 32→31 | 6 |
| 对照：把 `alias_model` 折到它自己的现值 | 45 | 3 | 3 | 32 | 0 |

恒等式 `canonical 减少的折叠数 == 唯一持有者的标签数` 两侧实测成立（`measured=5` `derived=5`，id 列表逐元素相同）。
纯归属折叠（阶梯四计数全不动、只有需求集移动）实测 2 处：`branching_base_url`、`hardcoded_symbol_roster`。
对照臂的 0 移动是判据要求的形状：它证明「上面那些移动」归因于折叠本身，而不是归因于把判据重跑了一遍。

## 全折叠臂 = 被推翻的那张面

一次 revert 全部 7 处折叠并摘掉 5 条新条目，得到拆分前那张面：
`{canonical_capabilities: 40, greedy_steps: 28, rows_expressible_today: 3, shipped_cover_rows: 6}`。
它的 `shipped_cover` 六行逐 id 为 `cboe-EquitySearch`、`intrinio-CurrencyPairs`、`intrinio-EquitySearch`、
`intrinio-HistoricalDividends`、`tmx-EtfInfo`、`tmx-EtfSearch`，其中**旧面认可能新面不认可**的三行是
`intrinio-EquitySearch`、`intrinio-HistoricalDividends`、`tmx-EtfSearch`——这就是 §39 里那句
「这 6 行已由工具自身的 `shipped_cover_rows` 发布（3→6）」的多出三行。
`rows_expressible_today` 在两侧都是 3，所以假读数从来不在普查的手声明里，只在 `shipped_cover_rows` 这一个派生计数里；
`greedy_steps` 28→32 与 §39 记的「build pool 28 步不变」是同一件事的两端：拆分本身加了 4 步。

## 拆代之后的阶梯恒等式（从 `capability-roadmap.json` 复算，不是探针名义）

- `rows_unlocked_by_greedy_prefix 188 = shipped_cover 3 + Σ(32 步 rows_newly_unlocked) 185`
- `rows_pending 235 = 累计解锁 188 + 仍锁 47`
- 退化对依旧成立：`rows_still_locked_after_engine_pool 47 == rows_outside_engine 47`（`rows_with_empty_need_set 0`）
- `rows_with_no_engine_work_possible 12`
- registry 状态分组两侧实测（`registry-status-both-faces.py --presplit …`，rc=0，摘要见
  `registry-status-both-faces.txt`）：拆分前 `present 6 / absent 33 / drifted 0 / unanchored 1` = 40 条 =
  40 canonical；拆分后 `present 6 / absent 38 / drifted 0 / unanchored 1` = 45 条 = 45 canonical；
  增量逐组为 `absent +5`、`present +0`、`drifted +0`、`unanchored +0`。
  两侧各自的恒等式「分组求和 == registry 条目数 == canonical id 数」都成立，`present` 一个未动——
  这就是「本轮没有任何能力因拆分而变成可用」的面证。
  §39 里那句「registry `present` 6 / `absent` 34 不变」同时把 `unanchored 1` 混进了 `absent`，实测是 33；
  该句一并由上面的两侧分组替换。边界照实写：这组分组是在**拆分前那张表 + 当前 238 行分母**上量的，
  150 行那一代的同名读数本轮没有重取，所以本节只说 §39 的那个值与它声称的那一代不合，不声称它在哪一代错。

阶梯前五位换成了 `flow.composite_key_segment 9/12, path.explode 3/15, param.value_mapping 4/19, path.object_keys 4/23, flow.fan_out 4/27`。
`param.value_mapping` 进了前五位是这次拆分的直接后果：它此前被折进 `columns.rename` 而不占任何阶梯位置，
现在它单独解锁 4 行。

## 本轮未做与边界（不写成已做）

- 新 5 条 registry 条目全是 `claim=False`：它们钉的是**限制**的锚点（`ParamSpec.enum`、`ParamSpec.query_key`、
  `ColumnSpec.nullable`、`ModelSpec.params`、`ModelSpec.path`），不是任何已出厂实现，所以本节不声明任何能力已具备。
- `census-merge.md` 与 §39 里「canonical 40→40」「出厂覆盖 3→6」两句已被本节推翻，原文按记录纪律保留在原位并被指针标注，
  被推翻的值仍在上面的表里可读。
- 本轮 `--write` 之前先把 `census-audit.json` 取回 `8a49a42`（C75 代）的内容再重跑一次，
  以免第二次 `--write` 把 `previous_values_before_c86` 从「C75 的值」吃成本轮自己的值；
  被覆盖的中间态（40-canonical 面，摘要 `0fe43ab5f74c5da2`）由提交 `912f59b` 永久保留，不在工作树里。
- 5 个引擎内核子代理的产物只在本节之外登记为「待复验」，`opendata/data/providers/_engine/{key_composition,
  multi_request, request_body, value_transform}.py` 有测试文件，`row_expansion.py` 本轮尚无测试文件；
  内核尚未接进 `spec.py`/`http_json.py`，所以本轮没有任何能力因它们而翻成 `present`。
