# C124 · batch5 逐文件字节围栏的两格重基（2026-10-17）

围栏：`tests/fixtures/registry_batch5_snapshot.json` 的 `source_hashes_sha256`（31 条），判定者是
`tests/test_provider_domains.py::_batch5_identity_snapshot` —— `:1363` 先断言夹具自身载荷摘要，
`:1368` 再逐条把 31 个路径从盘上重算。摘要字面量落在两处：`tests/test_provider_domains.py:1355`
(`BATCH5_SNAPSHOT_SHA256`) 与 `tests/test_pipeline_jobs.py:1755`（同一枚硬编码）。

## 1. 现象与归因：四条红共用一个前提

红面（`python -m pytest tests/test_provider_domains.py -o addopts= -p no:cacheprovider -q`，
HEAD `e549539c`，本仓自研 famafrench `row_model` 声明尚未落地时实测）：

```
4 failed, 2 passed in 1.03s
FAILED test_production_domains_preserve_v1_legacy_entries_and_explicit_semantics
FAILED test_declared_domain_contracts_match_exact_registered_provider_models
FAILED test_exact_domain_identity_assertion_rejects_removed_and_added_domains
FAILED test_public_exports_match_reviewed_batch5_counts_and_types
```

四条的 `--tb=line` 逐条同一行：`tests/test_provider_domains.py:1368: AssertionError:
opendata/data/providers/finviz/models/price_performance.py` —— 一个前提咬四条面，不是四条独立的病。
`AssertionError` 的 message 就是 `relative_path`，所以夹具里第一条不匹配的路径总是先报错，
**它不能告诉你一共几条**；母数得逐条重算（见第 3 节）。

漂移来自我自己前两轮：

| 被钉文件 | 围栏里的旧值 | 等于哪一份 | 改它的提交 |
| --- | --- | --- | --- |
| `opendata/data/providers/fred/models/sloos.py` | `d960cb231bc4b9cc…` | `git show 40c6f315^:…` = `d960cb231bc4b9cc51f4df2f02111c7c18f630f9e223a67bda665908a36e0f39` | `40c6f315`（2026-10-16，wave 13/16 的 fred `row_model` 声明） |
| `opendata/data/providers/finviz/models/price_performance.py` | `273b0818f3e5471a…` | `git show fbc08357^:…` = `273b0818f3e5471a3213ccff01de2511f1ce2c4f8c11ed8bbba30b37ec519f0d` | `fbc08357`（2026-10-17，wave 18 finviz 三行） |

围栏本身最后一次重基是 `b15717b6`（2026-10-15）。两次 `row_model` 声明式改动都只动类型注解那一行，
却在刻意的字节钉上把四条面判红 —— 与 §59（C103）当年同一族：**语义面没动，围栏先过期**。

## 2. 写了什么（只动 4 处，字节数不变）

```
opendata/.../finviz/models/price_performance.py  273b0818f3e5471a… -> 2881070564803fee…
opendata/.../fred/models/sloos.py                d960cb231bc4b9cc… -> 639a81178ecb23bb…
夹具载荷摘要字面量（两处站点）                   786fb181884ea564… -> d434b9137178e0df…
```

每个新值都由 `hashlib.sha256(live_bytes)` 现算，没有一个是我手打的。

## 3. 结构闭合：改动确实只落在两枚十六进制值里

对 `git show HEAD:` 的夹具字节与工作树字节逐位比对：

| 读数 | 值 |
| --- | --- |
| 文档内 64-hex 值窗口（结构正则 `": "<hex64>"` 扫全文） | 45 |
| 逐位不同的字节数 | 119 |
| 这些字节是否全部落在某个值窗口内 | **True** |
| 被触碰的窗口数 | 2 |
| `source_hashes_sha256` 条目 / 变更条目 | 31 / 2 |
| 其余顶层键（含 `candidate_sha256`、`batch5_count_adjudication`）是否逐字节同 | True |
| 文件字节数差 | 0 |

母数为什么是 119 而不是 128：两枚摘要各自只有真正不同的那几位参与比对，同位相同的十六进制字符不产生差。

我第一版的这道闭合检查自己就错过一次：窗口下标按 `index(needle) + len(rel) + 5` 推，把键引号与
`": "` 的宽度算少了一位，于是打印 `confined_to_hex_windows=False`。**被证伪的是我的仪器，不是这次编辑**
—— 改成用结构正则从全文取 `[0-9a-f]{64}` 值窗口后同一份字节读出 True。

## 4. 双侧反例：重基之后围栏仍然咬得住

`probes/b5shadow_rearm.py`（`runs/two-sided-arms.txt`，exit=0）在 `/tmp` 起两个影子根：`opendata/` 与
`docs/` 走符号链接，影子 `tests/` 只放一份影子夹具和一份影子测试副本，仓库内一个字节都不写。
arm A 把一条**当前与盘相符**的钉值（受害者由实测 census 选，本次为
`docs/evidence/C100/acceptance-fred-sloos/runs/…/receipt.json`）就地改成 64 个 0，并把影子副本的载荷摘要
字面量一起重基到影子夹具上 —— 否则红会落在 `:1363` 那道上锁的门，判不到 `:1368`（§59 第 3 节的教训）。

| 臂 | exit | 读数 |
| --- | --- | --- |
| arm_A_forged | 1 | `4 failed, 2 passed`，`first_bite: …/tests/test_provider_domains.py:1368: AssertionError: docs/evidence/C100/acceptance-fred-sloos/runs/20261012T181729.721761Z/receipt.json` |
| arm_B_inert | 0 | `6 passed`（同一装置、未篡改） |

`4 failed` 正是调用该快照的四条面，`2 passed` 是同一模块里不读围栏的两条 —— 反例的落点与母数都对得上。

## 5. 交叉核对：上一轮那台仪器重算同一件事

`docs/evidence/C103/batch5-source-pins/probes/pin_rebase.py`（不带 `--apply`，`runs/pin_rebase_dry_census.txt`）
把 31 条全部从盘上重算：`matched=31 stale=0 missing=0`，`snapshot_payload_sha_before=d434b9137178e0df…`，
并按它自己的闸门 `verdict: REFUSED`（没有过期钉就拒绝写）。它的 `exit=1` 是这台仪器的**拒写门**，
不是判定失败 —— 我在第一次归档时把这个码写成了未经测量的断言，重跑取 `$?` 后才确认 `pin_rebase_exit=1`、
`stdout_bytes=407`、`stderr_bytes=0`。

## 6. 本次 famafrench 声明的爆炸半径：0

`famafrench_pinned_paths=0` —— 31 条钉里没有一条 famafrench 路径，所以本轮 `row_model` 声明（以及随后
可能的 `_source.py`/`datasets.py` 改动）不可能把这道围栏判红。这条读数是顺手实测的，不是推断。

## 7. 重基后的绿面

| 面 | 命令 | 读数 |
| --- | --- | --- |
| 判定簇（4 模块） | 见 `runs/green_judge_cluster.txt` | `cluster_exit=0`，`117 passed in 1.39s`；分母 117 与记录第 2849 行（§59/C103）同一次读数的 117 一致，说明重基没有推动收集数 |
| famafrench + 域 + 作业 | `tests/test_provider_domains.py tests/test_pipeline_jobs.py tests/test_famafrench_provider.py` | `108 passed in 1.26s` |

## 8. 没主张的东西

- 不动任何判定语义：判定器一行未松，只是把两枚**由盘上现算**的摘要写回它自己的夹具。
- 不主张这三条红是"别人的"：两处漂移都是我的 wave 13/16 与 wave 18 提交造成的，缺的是提交前的这道重算。
- 不主张 plane 1 因这次重基而动：`OFFLINE_ACCEPTED` 计数与 `claim_count` 都不读这道围栏。
- 不主张全量套件的其余红面已被本轮触碰；本轮只把 `test_provider_domains.py` 那 4 条判到绿。

## 9. 复算

```bash
# 影子双侧反例（仓库零写入）
python docs/evidence/C124/batch5-pin-rebase-20261017/probes/b5shadow_rearm.py   # arms_exit=0
# 上一轮仪器独立重算 31 条
python docs/evidence/C103/batch5-source-pins/probes/pin_rebase.py               # exit=1 = 拒写门
# 判定簇
python -m pytest tests/test_provider_domains.py tests/test_alert_matrix.py \
  tests/test_alert_matrix_model_domains.py tests/test_pipeline_jobs.py \
  -o addopts= -p no:cacheprovider -q                                            # 117 passed
```

作废条件：(a) `registry_batch5_snapshot.json` 被再次改写而摘要字面量未同步；(b) `_batch5_identity_snapshot`
的 `:1363`/`:1368` 两道门被合并或放宽 —— 那第 4 节的臂就不再区分任何东西；(c) 影子根不再能把
`parents[1]` 解析到真仓（那 31 条逐文件重算就不是实测）。
