# C87 证据面：内核接线轮（composite_keys / value_map）与出厂后的代际对拍

本轮的开发动作只有一个：把 `ModelSpec.composite_keys` 与 `ParamSpec.value_map` 两个内核缝进
`spec.py` + `http_json.py`，并把对应 registry 条目改成 `read_from_disk=True`。本节发布由此产生的
四份可复算面；判定与归因写在 `开发验收记录.md` §41，这里只放载体与复算命令。

## 载体

| 文件 | 角色 | sha256 |
| --- | --- | --- |
| `docs/evidence/C87/roadmap-check-c87.txt` | `--round-id C87 --check` 全文 481 行，末行 `--check verdict: PASS` | `abc8db2dcdd8af42940b8df58a0e6772006b123e091f0a7cbf58bf22c41f4f9f` |
| `docs/evidence/C87/registry-status-both-faces-c87.txt` | 出厂面与 §40 拆分面并排；两侧闭合与 pre-split 载体 pin | `d1e82ef8d6cf49aa0326a395784ffb7d4d31e3f5a25470ea3a0a0f9f2e4b8689` |
| `docs/evidence/C87/label-split-probe-rerun-c87.txt` | §40 那台 revert 仪器在本代际上的逐字重跑（`SPLIT_PROBE folds=7 failures=0`） | `20ddc865dc90fc254d373cfff906d6f9307f0a15b3de4dbe8fd1806b986d23b1` |
| `docs/evidence/C87/suite-full-c87.txt` | 全量离线套件（无 `-x`）`22215 passed, 170 skipped in 470.14s` | `71d166ec731b183a0334ce27617a27f86af26e12dcf0506cbb719ee36cac60b5` |

`suite-full-c87.txt` 的末行 `rc=0` 由运行外壳追加，不是 pytest 的输出行；`170 skipped` 含全部需要
`OPENDATA_ALLOW_LIVE_E2E` 精确 token 的 live 腿，本轮未设置该变量，所以这些 skip 不计入离线绿灯。

## 复算命令（按顺序）

```sh
python scripts/quality/model_capability_census.py --round-id C87 --check
python scripts/quality/model_capability_census.py \
  --json docs/迭代计划/迭代2-统一Provider架构与全量能力补齐/capability-roadmap.json --round-id C87
git show 912f59b:scripts/quality/model_capability_census.py > /tmp/c86_pre_split.py
shasum -a 256 /tmp/c86_pre_split.py | cut -c1-16      # fe5a3a55ef600b7f，与两份档案的 pin 同一
python docs/evidence/C86/registry-status-both-faces.py --presplit /tmp/c86_pre_split.py
python docs/evidence/C86/label-split-probe.py --probe
python -m pytest -q --no-cov -o addopts="" \
  tests/test_engine_key_composition.py tests/test_engine_value_map_and_composite_path.py \
  tests/test_engine_row_expansion.py tests/test_engine_multi_request.py \
  tests/test_engine_value_transform.py tests/test_engine_body_request.py \
  tests/test_engine_row_filters.py tests/test_oecd_engine_provider.py \
  tests/test_model_capability_census.py
```

第二条命令会重写 `capability-roadmap.json`；先跑它是为了确认本轮档案对应的就是 `C87` 那一代，
再跑第一条。本轮归档的 check 面是 `--check` **不带** `--json` 跑的，因此里面没有 `READ-ONLY` 行
（我起草本节时先按「check 面含 READ-ONLY」写过一次，`grep -c READ-ONLY` 对该档案读回 0，已改成
现在这句）。check 模式不落盘这件事不由这张档案证，由 `tests/test_model_capability_census.py`
的 `test_check_mode_leaves_the_artifact_it_verified_untouched`（断言 `"READ-ONLY" in printed`）证，
本轮该文件与 `tests/test_oecd_engine_provider.py` 合跑 `44 passed`。上面第一条 check 面即本轮归档
那份；本轮 roadmap 摘要 `sha256[:16]=f7c1d2385361cf9d`。

## 本轮改动的三份 C86 仪器字节（重钉）

格式化与 mypy 修复动了 `docs/evidence/C86/` 下三个探针的字节，本轮重钉并把「git 对象摘要」与
「工作树摘要」分开记：

| 文件 | 本轮 sha256[:16] | 说明 |
| --- | --- | --- |
| `docs/evidence/C86/label-split-probe.py` | `970d29585b30aae2` | 首行 carrier pin `fe5a3a55ef600b7f` 钉的是 `912f59b` 的 blob，不是这两个字节 |
| `docs/evidence/C86/census-merge-audit.py` | `96a937031b3ecc35` | 自检 `arms=5 failures=0 copied_trees=5` |
| `docs/evidence/C86/registry-status-both-faces.py` | `c3e1913926d4c470` | `--presplit` 收的是模块文件而非 `label-map-before-split.json` |

`scripts/quality/model_capability_census.py` 的工作树摘要为 `fb9bcbd47b742554`；它与上面 `fe5a3a55…`
是两个不同的断言（现盘 vs git 对象），任一方被引用时必须说明引用的是哪一个。

## 本轮刻意没做的事

- **不重刷 `census-audit.json`**：引擎接线不改变任何 `census-*.json` 的字节，重刷只会把该文件里的
  `previous_values_before_c86`（C75 代的值）吃成本轮自己的值。
- **不给任何 provider 行记 DEV_DONE**：`declaration scan` 实测 `composite_keys=` 与 `value_map=` 的
  生产声明数为 0，`built_but_unused` 现列 `decode.archive_member`、`flow.composite_key_segment`、
  `param.value_mapping`。「14 行可声明」是阶梯归属的变化，不是实现的完成。
- **未接线四个内核**：`row_expansion` / `multi_request` / `value_transform` / `request_body` 的生产
  importer 数为 0（只有各自的测试文件引用它们），所以 `path.explode`、`flow.fan_out`、`request.body`、
  `columns.rescale` 仍留在 `absent` 与 build pool 里。
