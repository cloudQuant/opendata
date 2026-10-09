# C85 覆盖率报告归档（AC-17|06 的判定对象）

- 生成命令：`make test-cov`，随后由 `scripts/quality/coverage_archive_stamp.py` 归档
- 归档时间：2026-10-10T07:06:54+0800
- 测量时的 HEAD：`f6c3ec475ea32c925622191441308d272378e3bb`
- `xml`：`docs/evidence/C85/coverage-final.xml`（现盘 `coverage.xml` 的逐字节副本）
- `html`：`docs/evidence/C85/coverage-final-html.zip`，400 个页面

## 戳记里记录的聚合数（由归档副本重算，不是现盘文件的重述）

- 语句：23232 / 25427
- 分支弧：6525 / 7684
- statement+branch 合并覆盖率：89.87%

## 各判定总体

| 总体 | 文件数 | 合并覆盖率 | 解析方式 |
| --- | --- | --- | --- |
| `new_code` | 290 | 89.87% | A2 新代码与 coverage source 的交集 |
| `opendata/data` | 191 | 90.17% | 目录前缀 |
| `pipeline` | 39 | 96.38% | 路径成分（opendata/pipeline/） |
| `opendata_fuyao` | 9 | 93.79% | 历史身份映射（opendata_fuyao 由 3f05f636c 删除，11 个历史文件） |

## 探针怎么用这份归档

- 判定的数字取自本目录的 `coverage-final.xml`，不是取自 gitignore 的现盘报告；改动其中任何一个计数会同时打断「逐行重算 vs coverage.py 自报」与「戳记 vs 归档现状」两组等式。
- 现盘 `coverage.xml` 只作为可复现见证被读取：与归档的最大百分点差受 `drift_within` 约束，不要求逐字节相等——第 12 员在第 11 员之后才重跑覆盖率。
- 戳记与 HEAD 的关系由两条独立等式给出：`stamp_ancestor` 说 commit 在 HEAD 历史上，`tree_identity` 说被计量的源码根两边同一棵 git 树。
- 退役目录 `opendata_fuyao` 不靠目录前缀命中：先由删除提交逐文件枚举，再过 `source_layout.py` 的历史身份映射落到存活路径，映射不上或落空者各自计数。

## 重新生成

```
make test-cov
python scripts/quality/coverage_archive_stamp.py
```
