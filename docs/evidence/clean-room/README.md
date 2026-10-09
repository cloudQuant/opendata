# 逐包清洁室审查档案（AC-16|01「审查留档」的载体）

这里是 `scripts/quality/acceptance_item_probe.py` 的 `canonical_clean_room_records()` 唯一会读取的目录。
判据原文里括号点名的「人工抽查比对并留档」需要一个有路径、有字段、可重算的载体；C77 之前它没有，
判定退化成在 `docs/evidence/**` 里找一行同时提到包名和声明句的散文——实测那条腿把缺口自己的复述
（`C64/remaining-items-audit.md` 里「提交说明带「无 OpenBB 源码参照」的 **0** 个」）和 C65 抄进去的
探针读数当成了记录，记了 7 个包 / 8 行，而真实审查数是 0。

## 一包一份档案

文件名固定 `docs/evidence/clean-room/<包名>.md`，`<包名>` 必须与 `provider_packages()`  census 出来的
目录名一致（C77 实测 12 个：见下方 census 命令的输出）。路径不对即拒收——同样一份写全了的文字放在
别处不算留档，这正是旧口径的失效形状。

必须跟踪进 git（`git ls-files` 读不到就拒），必须逐包一份（一份总览文档不认）。

## 字段与判定

档案里要有下面五行字段（`键：值` 同行），外加一行审查结论。任何一栏不合，`recorded_pkgs` 不加，
并在读数里打印该包的拒因（不是静默跳过）。

| 字段 | 要求 | 拒因示例 |
| --- | --- | --- |
| `审查日期` | ISO 日 `2026-10-09` | `审查日期不是 ISO 日（读到 ''）` |
| `审查人` | 非空且不指向机器 | `审查人缺位或是自指` |
| `覆盖面` | 以整数开头，等于该包实测 `.py` 文件数 | `覆盖面写 '3 个 py 文件'，实测该包 2 个` |
| `代码面摘要` | 64 位十六进制，等于该包当前字节的摘要 | `代码面摘要与该包当前字节不符（档案审的不是现在这份代码）` |
| `方法与反证` | 写实，≥12 字 | `方法与反证一栏没写实` |
| 结论行 | 同一行含 `审查结论` + 包名 + 声明句，且不含否定式标记 | `否定式披露不算声明` |

声明句是 `docs/proposals/openbb-migration/README.md` 里的那句（常量 `CLEAN_ROOM_PHRASE`）。
结论行模板：

```
审查结论：<包名> 为自研实现，无 OpenBB 源码参照。
```

否定式标记（`无法`/`未能`/`不能证明`/`未审查`/`待补`/`缺口`/`冒充`/`不构成`）出现在结论行里，
这条档案就是**披露**而不是声明——披露同样有价值，但它不该让判据变绿。
正文任一处带探针回声标记（`VERDICT `、`判据原文：`、`本探针：`、`台账现状：`）直接拒收：
一轮不该因为自己打印过一句读数就领到审查记录。本节是在**列举**这些标记，所以别把本节整段抄进档案，
抄过去档案就自己带上了被拒的那个字面量。

## 覆盖面与代码面摘要怎么算

```
python3.11 -c "
import sys; sys.path.insert(0, '.')
from scripts.quality.acceptance_item_probe import load_context, provider_packages, provider_py_files, provider_py_digest
ctx = load_context()
for name in provider_packages(ctx):
    files = tuple(provider_py_files(ctx, name))
    print(name, len(files), provider_py_digest(ctx.root, files))
"
```

摘要按 `路径\0字节\0` 顺序拼该包全部跟踪且落盘的 `.py` 算 sha256，`覆盖面` 用同一个 `len(files)`。
所以代码一旦改动，旧档案会自动失效——这是钉住字节的目的，不是缺陷。重算后另存一份新档案
（或把摘要改写成新值并重新审查），别改历史。

## 现状（C77 实测）

本目录除本 README 外没有任何 `<包名>.md`，AC-16|01 与 AC-16|02 读数为
`recorded_pkgs=0 / record_refusals=12`，两条目均为 `gap`。重算：

```
python3.11 scripts/quality/acceptance_item_probe.py --item "AC-16|01" --item "AC-16|02"
```

能给出「无 OpenBB 源码参照」这句话的包需要有人真的逐文件读过并对照过上游同名 provider 的函数清单；
本轮没有做这个动作，所以没有写档案。判定探针能否认可合规档案，由
`docs/evidence/C77/clean-room-record-schema-face.py` 的 affirmative 臂在临时仓库里证明
（它必须能把一个写全了的假档案记上，否则 0 只是一个永远为 0 的尺子）。
