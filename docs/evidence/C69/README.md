# C69 — 分页判据与留档链的导入面

本轮两件事，都带反向对照。

## 1. 引擎分页三处缺陷（D1/D2/D3）修好，并留下「同一测试跑在两版引擎上」的读数

`opendata/data/providers/_engine/http_json.py`：

- D1 `_page_request` 覆写调用方显式声明的页大小 → 只在缺失时注入；
- D2 `fetch_pages` 用「声明默认页大小」当短页尺子 → 新增 `effective_page_size()`，量请求里真正带的
  `limit`（今天 D2 被 D1 掩着：D1 先把请求改回默认，两把尺子恰好相等；单修 D1 会让 D2 变活）；
- D3 `transform_data` 把各页公布的 total 相加，并把 0 当成没公布 → 改成「页间必须一致，行数必须等于
  那个一致值」，新增 `{PREFIX}_TOTAL_CONFLICT`（页间不一致、或行数多于公布总数），短于总数仍报
  `{PREFIX}_INCOMPLETE`。

反事实面：`paging-defect-counterfacts.txt`。同一个测试文件，臂 A（修好的引擎）65 passed / 退出码 0，
臂 B（`git show HEAD:` 的引擎）10 failed 55 passed / 退出码 1。10 条失败按三类分开登记——4 条是旧读法
的行为反例，6 条只是「新 helper 在 HEAD 上不存在」，2 条守卫两边都绿。把这三类混成一个数字就没有
信息量，所以分开写。

## 2. 留档脚本的退休导入：用 AST 数清，不再用 grep 数

`retired-import-census.py` / `.txt`：扫 `docs/evidence/**/*.py` 69 个文件，把「真导入」和「字符串常量里
的提及」分成两箱——`files_with_real_imports=11`、`import_sites=20`、`files_with_string_only_hits=17`、
`target_missing_on_disk=0`、`refused_by_authority=5`。导入映射交给仓库自己的布局权威
（`scripts/quality/source_layout.historical_identity`），这里不写第二张表；权威拒绝时把拒绝原话打出来。
退役名可用性用 `find_spec` 量（不执行导入），正向对照是 `opendata resolvable=True`，两个退役名都 False。

这条推翻我上一轮的口径并已在文里更正：C67 README 说「其余 13 份仍导入退役顶层名」，AST 数是 11 份
20 个站点（C54 本轮已修好且重算逐字节相同，C44 那处命中在 fixture 源串里、从来不是导入）。同一份
档案里的处置遵循 C68 立的规则：**证不了逐字节相同的修复一律不动**，所以 11 份里现场型脚本（名字带
live、或当日取数）保持原样，只有 C54 的修复被证明并保留。

## 复算

```shell
python3 -m pytest tests/test_provider_engine_contract.py -q --no-cov -p no:cacheprovider
python3 docs/evidence/C69/retired-import-census.py
```

两条都离线；第一条把引擎发送面 monkeypatch 成合成 transport，第二条只做 AST 解析与 stat。
