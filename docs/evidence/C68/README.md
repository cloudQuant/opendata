# C68 — 档案脚本的退役导入：能证的修一份，证不了的两份按规则不动

`retired-import-repair.txt` 逐份记了三份档案的负臂读数、修复动作、修复后重跑与逐字节比对。
立的规则是：**修复必须由「重算输出与档案 body 逐字节相同」证明，证不了的一律回退。**

| 档案脚本 | 负臂（原样先跑） | 比对 | 处置 |
| --- | --- | --- | --- |
| `C54/caliber_pivot_face.py` | rc=1，`No module named 'opendata_fuyao'`（第 159 行） | 修好第 159/210 两行后 rc=0，body 4988 字节 `sha256=5fca1ef0…` 与档案逐字节相同 | **保留修复** |
| `C28/graded-shapes-render.py` | rc=1，同一模块缺失（第 46 行） | 修好导入后 rc=0，但 body 7213 字节 ≠ 档案 7160 字节 | **回退**，脚本与档案都保持原样 |
| `C44/census_counterfactuals.py` | rc=0，本来就跑得通 | body sha 与档案相同，零编辑 | **拒绝修改**——第 50 行的 `opendata_fuyao.endpoints` 在 `_SHELL_CASES` 的 fixture 源串里，只被 `ast.parse` 当自样本读，改它就改了这份脚本判的东西 |

C28 的差异不是导入修坏了：`FUYAO_PERMISSION` 的文案在 C28 录制（2026-09-26）之后被合法改写过，
现值取自 `opendata/data/providers/ths/transport/error_messages.yaml:41`。分类档位、归因、泄漏计数
逐行未变，只有那一行文案与档案里那行不同，所以逐字节这条过不去。另：档案 body 末行的 `exit=0`
是采集器附加的页脚，不在脚本 stdout 内——比对口径要把这行单独说明，不然会把页脚当成脚本差异。

## 主会话对本份回执的独立复核（子代理的读数当线索，不当结论）

- `docs/evidence/C69/retired-import-census.py`（AST 分箱）确认 C54 的导入已不在场、C44 那处命中确实
  落在字符串常量箱里（`files_with_string_only_hits` 而非 `files_with_real_imports`）。
- C54 由主会话独立重跑：`PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=. python3 -u docs/evidence/C54/caliber_pivot_face.py`
  → 退出码 0，stdout 4988 字节 `sha256=5fca1ef06972979d…`，与档案正文（去掉 10 行 `#` 抬头后的 body）
  完全相同 ⇒ **IDENTICAL 成立**。
  第一次复核我把它读成 DIFFERENT，两处都是仪器错：命令里漏了 `PYTHONPATH=.`（脚本自己报
  `No module named 'opendata'`，与退役名无关），分箱标记又用了 C28 那份的分箱词；正文起点应是
  第一行 `=== 面 A` **及其前面的空行**，少一行就是 4987 字节的假差异。
- C28 的文案漂移独立复核：
  `PYTHONPATH=. python3 -c "from opendata.data.providers.ths.transport.errors import error_for_upstream_code as f; print(f(2003))"`
  → `FUYAO_PERMISSION: 权限不足，或凭据无效/已吊销（录制实测该形态回的是本码而非 2001）`，
  而档案第 21 行是 `…: 无该数据端点的访问权限` ⇒ 回退决定成立。
