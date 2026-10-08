# C67 — 棘轮范围面按布局权威分层；AC-17|03 的判定从「不可达」回到可复算

本轮不做新判据、不放宽任何判据，只做两件事：把 AC-17|03/|05 的「范围只降不升」量成布局权威
口径下的同一个种群，并把 C66 搬迁顺手打断的一处档案复算面接回去。台账 130 项、文档勾选与
判据原文均未改动。

## 1. 为什么要动这台仪器

`measure_ac17_03` 原来用 `py_files_under(root)` 直接走 `opendata/`，而搬运树
`opendata/data/providers/akshare/_vendor/` 嵌在 `opendata/` 里面，于是自研存量计数把 325 个搬运
文件也算进自研债：`ruff_dark=325`、`mypy_dark_outside_legacy=325`、`bandit_dark=325` 三个读数
永远为红，且任何真实清理都不会让它变绿。按 self-test 的合同，这意味着「本探针声明的任何读数
都无法让判定通过」——即这台仪器自己把 AC-17|03 判成不可证明，而原因不是产品缺债，是两个不同
种群被加在了一起。

修法是引入 `first_party_py_files_under()`，分层交给布局权威 `scripts/quality/source_layout.py`
的 `classify_path()`，而不是在本文件里再抄一份前缀表。搬运文件仍然是 AC-17|05 的分母，一处也没
被丢掉：`tests/test_acceptance_item_probe.py::test_selfdev_population_leaves_the_nested_vendor_tree_out`
断言 `naive - first_party == vendored`、同姓的首方适配层
（`opendata/data/providers/akshare/__init__.py`）仍在首方集合里。

## 2. 「只降不升」还差一条关系

快照历史上 `opendata_fuyao` 这个根在 3cf0cf7→3f05f63 退出名单，但它的文件没有消失：九个 `.py`
被分发进了 THS provider（`scripts/quality/source_layout.py:_FUYAO_PATHS` 逐文件登记了这个映射）。
原来的 `scope_vanished()` 只做键差集，把这算成「一个被测根从名单里消失」，于是 AC-17|03 唯一的
剩余红灯是 `vanish_recent=1`。

现在一条根要同时满足两条才被叫作消失：在 `canonical_counts()` 归一后的键里找不到对应物，**且**
新 census 的总文件数补不回它失去的计数。真正的 exclude 吞根会留下恰好等于该根计数的缺口，所以
这个改写没有把「被吞掉的根」放过——同一测试用 `flat`  census（少 12 个）复验它仍然报红。
被赦免的搬家改为打印：新事实 `reparented` / `reparented_detail`，|03 与 |05 两处读数都打印
「从名单退出但文件计数在他处回来的 N 个」。

## 3. 复算命令与读数

```shell
python3 scripts/quality/acceptance_item_probe.py --self-test      # 见 probe-self-test.txt
python3 scripts/quality/acceptance_item_probe.py --item 'AC-17|03' --item 'AC-17|05'
python3 scripts/codemod/verify_no_akshare.py                     # OK: 630 walked, 无新引用
python3 -m pytest tests/test_acceptance_item_probe.py -k "scope_vanished or selfdev_population or ac5_02"
python3 /tmp/ac1703_clauses.py                                   # 逐条子句真值（仪器，非判据）
```

- `probe-self-test.txt`：`OK: 115 probe(s) measured; every judge is reachable from its declared
  repair, and all 746 counterfact(s) flip a clean reading back to a gap.`
- `probe-ac17-03-05.txt`：两条 `VERDICT … proven`，`census: proven=2`。|03 的关键读数
  `680 磁盘 / 680 ruff 走到 / 看不见 0`、`mypy 300/318 且被排除的 18 个全在棘轮点名的遗留区`、
  `bandit 381/381`、`触碰 656 ↔ A2 集 656（a2-check exit 0）`、
  `抬高存量上限 0 次`、`近 2 次转换消失的被测根 0`、`从名单退出但计数在他处回来 1 个
  （3cf0cf7->3f05f63 opendata_fuyao(9)）`。
- `c44-golden-recompute.txt`：C66 删掉顶层 `opendata_fuyao` 后，`docs/evidence/C44/golden_millis.py`
  这条档案已不可运行（反面：`import opendata_fuyao` rc=1）。导入按 `historical_identity()` 指向
  `opendata.data.providers.ths.endpoints` 后重跑 rc=0，且正文与档案
  `docs/evidence/C44/golden-millis.txt` 标记行以下 **逐字节相同**
  （两侧均 246 字节，sha256 前缀 `be2f505254aa`）。档案正文未被改写，改的是脚本的一行导入与
  一句说明。

## 4. 本轮没有做的事

- 不改判据原文、不改台账 state、不动 130/350/34/202/19WP/25AC 任何分母。
- `vanish_older=1`（4ee1bbc→2b68225 `akshare` 394→131）是历史上一次真实的范围收缩，只作待复核
  登记打印，不当红灯、也不当已通过。
- 其余 13 份档案脚本仍导入退役的顶层名（`opendata_fuyao` / `opendata_http`；`grep -nE
  '^\s*(from|import)\s+(opendata_fuyao|opendata_http)' docs/evidence` 复算），本轮只接回了能
  逐字节复算的那一处；需要真实调用的探针保持原样，不改写历史档案。
- 未 push。CI 看到的是 `origin/dev`，本地证据不等于 CI 通过。
