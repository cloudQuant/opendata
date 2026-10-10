# C86 — 把 12 个 provider 的 88 行普查并入路线图分母（150 → 238）

本节记录一次分母变更及其可复算面。所有数字来自本目录的载体文件，不由本文件声称。

## 载体

| 文件 | 内容 |
| --- | --- |
| `census-merge-audit.py` | 判定探针：census ↔ 350 行台账逐行 join + 五条篡改对照臂 |
| `census-merge-selftest.txt` | 五条对照臂的实测（`live=X -> tampered=Y`）与实时面，`rc=0` |
| `census-merge-write.txt` | `--write` 的实时面与 `census-audit.json` 写前/写后摘要 |
| `census-per-file.txt` | 17 个 census 文件逐文件的行数、carried/NEW、provider 集与 claimed/actual 分歧表 |
| `roadmap-check-c86.txt` | `model_capability_census.py --round-id C86 --check` 全文（461 行，末行 `--check verdict: PASS`） |
| `roadmap-before-after.txt` | 两代 `capability-roadmap.json` 的逐字段对照（含两代摘要） |

复现：

```bash
python docs/evidence/C86/census-merge-audit.py --self-test
python docs/evidence/C86/census-merge-audit.py --write
python scripts/quality/model_capability_census.py --round-id C86 --check
```

## 分母

- 台账 `docs/迭代计划/迭代2-统一Provider架构与全量能力补齐/模型级任务清单.csv`：350 行、32 个 provider；本轮未改写它。
- census：17 文件 / 238 行 / 238 唯一 `task_id`（carried 3 文件 150 行 + NEW 14 文件 88 行，`census-per-file.txt`）。
- join：`in_census=238`、`id_collisions=0`、`unknown_ids=0`、`ledger_field_mismatches=0`（provider 与 upstream_model 逐行对台账）。
- 册外 `out_of_scope=112`，组成为 `DEV_DONE 2 / IN_PROGRESS 13 / NOT_RUN 97`；其中 110 行属 4 个尚无 census 文件的 provider（`fmp 69`、`fred 36`、`ecb 3`、`bls 2`），另 2 行是已有 census 文件的 provider 内 `DEV_DONE` 的 cboe 行（`OBB2-cboe-AvailableIndices`、`OBB2-cboe-IndexConstituents`）。

## 五条对照臂

探针的每个计数都必须在被篡改时移动，否则该零只是探针打不开。实测（`census-merge-selftest.txt`）：删除最后一个 census 文件 `census_files 17 -> 16`；改一个 `task_id` 使 join 落空 `unknown_task_ids 0 -> 1`；抬高一个 `provider_groups` 声明数 `mismatches 0 -> 1`；把某行的 provider 换成台账不认的值 `ledger_field_mismatches 0 -> 1`；删一行行体 `census_rows 238 -> 237`。`armed=5 failures=0`，且五臂都跑在临时复制树上，实时 census 未被写。

## 守卫的移动方式

`EXPECTED_CENSUS_ROWS` 从 150 改到 238，同时删掉 `CENSUS_CARRIED_FILES` 与其 `carried_forward_files` 输出键——那份「本轮沿用的三个文件」清单是按名字硬写的，任何一次并入都会让它静默变成假话；文件集合本来就由目录 glob 实测并写进 `census.files`，不需要第二份声明。行数守卫保留，且 `tests/test_model_capability_census.py` 里留了一条刻意写死的 `assert expected == 238`：普查再增长必须有人主动改这一行，而不是把阈值放宽。守卫自身两侧可见：篡改臂给出 `238 -> 237` 的 `fired=True`，实时面为 `measured=238 EXPECTED_CENSUS_ROWS=238 fired=False`。

## 路线图重derive：加法没有扩充能力集，只改变了顺序

`roadmap-before-after.txt`（C75 代 → C86 代）：原始标签 94 → 95（88 行只带来 1 个新拼写）、提及 357 → 764、canonical 40 → 40、build pool 28 步不变、集合内能力名两侧集合差为空；registry `present` 6 / `absent` 34 不变。阶梯前五位由 `flow.composite_key_segment 9/12, path.explode 4/16, path.object_keys 3/19, shape.columnar 3/22, columns.rescale 2/24` 变成 `flow.composite_key_segment 9/15, path.explode 6/21, flow.fan_out 5/26, flow.branch 7/33, flow.join 5/38`。累计解锁 111 → 188，仍锁 36 → 47，`rows_outside_engine` 36 → 47，纯非引擎行 12 → 12。

三条恒等式实测成立（`roadmap-before-after.txt` + 上面的面）：`pending 235 = 累计解锁 188 + 仍锁 47`；`188 = 出厂覆盖 6 + 28 步逐行求和 182`；本轮 88 行恰好被两个面吃尽 `88 = (188-111) + (47-36) = 77 + 11`。

## 两处由实测得到、不写在探针名义里的结论

1. 纯非引擎行前后同为 12，且逐 id 相同（`stockgrid/ShortVolume`、9 个 `yfinance/*`、2 个 `finviz/*Performance`）——不是探针没数到新行，而是这 88 行每一行的需求集里都至少有一个引擎能力，所以全部落在引擎池内或已出厂。
2. `rows_still_locked_after_engine_pool` 与 `rows_outside_engine` 今天的 id 集合完全相同（47 = 47，两侧差集皆空）。两者定义不同：前者是「引擎池耗尽仍锁」，后者是「需求集与非引擎能力相交」。它们只有在某个**引擎**能力的锚点漂移（`status=drifted`）时才会分开——漂移项被排除出池但不计入非引擎。当前唯一无锚项是 `outside.source_absent`（`UNPROVEN` 打印的那一条），它本身就是非引擎，所以两面退化成一个面。这是读数，不是缺陷；记下它是为了让下一次「两数不等」被当作漂移信号读，而不是被当作噪声。

## expressible_today 的手claim与registry实测

手声明 `true` 3 行、registry 判 `true` 9 行、分歧 6 行，其中 3 行是本轮新行（`OBB2-intrinio-CurrencyPairs`、`OBB2-intrinio-EquitySearch`、`OBB2-intrinio-HistoricalDividends`，只缺 `columns.rename` 和/或 `rows.filter`，两者均已出厂）；另 3 行为 carried 的 `OBB2-cboe-EquitySearch`、`OBB2-tmx-EtfInfo`、`OBB2-tmx-EtfSearch`。这 6 行已由工具自身的 `shipped_cover_rows` 发布（3 → 6），未新增载荷字段。反方向（手 `true` 而 registry 判不能）实测为 0：手 `true` 的 3 行需求集为空，空集是任何集合的子集，因此两侧同判——这也是先前探针把它们的空需求误报成分歧的原因。

## 未证与边界

`census-audit.json` 的 `c86_reaudit.scope` 逐字写明本轮**没有**重测的字段（`engine_audit`、`registration_check`、`spot_checks`、`c74_reaudit`、`uncovered_providers`），因此这 88 行的标签正确性依据只有两条：taxonomy 的 fail-closed（未映射标签直接中止运行）与本轮的逐行台账 join；没有对 88 行逐行重读上游源码做 oracle 复核。上游 HEAD 与台账钉的 commit 一致（`3e071fcc2cd9…`，`upstream_commit_ok=True`），证据根计数 `repo_root 28 / upstream_root 210`（上一代在 150 行上为 122/28）。真实来源调用与 `live_verification_status` 一律未触及：350 行仍全为 `NOT_RUN`。
