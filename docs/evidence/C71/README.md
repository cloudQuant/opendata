# C71 — AC2-10 逐模型判定面：把引擎契约做成可参数化的面，并证明它会翻

## 这一包留了什么档

1. `tests/test_provider_model_contracts.py`（提交在仓库测试面，不在本目录）：把 AC2-10 的
   契约判定从「一个 demo spec 走一遍」升级成「对每一个已注册的 `ModelSpec` 声明自动生成一遍」。
   哪些格子适用由声明自己决定（有没有必填参数、有没有数值列、翻页形态、凭证、路径模板），
   夹具从声明的 kind 与 `rows_pointer` 合成，模型不能靠「写了一条对自己有利的测试」过关。
2. `suite-flip-counterfacts.py` + `suite-flip-counterfacts.txt`：同一组 node 在六次人为破坏下的
   实测读数（每次一个规则），加上「不破坏」对照与还原证明。
3. `face-applicability-census.txt`：19 个判定格子的适用面计量，按**注册总体**与**探针总体**分开打印。

## 实测读数（本文件生成当刻）

- 参数化后本文件收集 158 项；与 `tests/test_provider_engine_contract.py`、
  `tests/test_cboe_engine_provider.py` 合跑为 `214 passed, 23 skipped`，`rc=0`。
- 注册总体：`bindings=46 engine_driven=2 hand_written=44 sources=1`。
  即 350 个 provider×model 任务里，走声明式引擎的仍只有 2 个，且都来自 cboe。
- 19 个格子全部被至少一条声明判定；其中 `from_registry=0` 的有 10 个：
  `credential`、`date_column`、`bool_column`、`str_list_column`、`enum_param`、`paged`、
  `page_kind`、`offset_kind`、`cursor_kind`、`declares_total`。这 10 格由 3 条探针声明承担，
  **不是**由已实现的原厂任务承担；反过来 `empty_pointer` 与 `path_template` 有注册总体自带的一半。

## 判定面为什么需要探针

只跑注册总体会得到一串 `skipped`：两条 cboe 声明都不带凭证、不翻页、不发布总数。
「没有声明带这个能力」和「带这个能力的声明没被判定过」是两件事，前者是源任务缺口，
后者才是引擎缺陷。把两者混在一起，绿色就变成缺席的读数。`TestApplicabilityCensus` 按
`from_registry` / `from_probe` 分列打印，并要求每个格子 `judged >= 1`，这样一旦某个声明字段
变得读不出来（例如路径模板永远解析不出占位符），该格会在两个总体上同时归零而报错，
而不是安静地变成 skip。

## counterfact：六条腿各自该翻在哪里

| 腿 | 破坏的规则 | 预期失败的判定 | 实测 |
| --- | --- | --- | --- |
| 对照 | 不破坏 | 无 | `rc=0 failed_lines=0` |
| D2 | 短页改用「声明默认页大小」判断，而非请求里真正带的值 | `test_caller_set_page_size_survives_into_every_request` | 1 failed |
| D3 | 页间总数重新求和 | 重复总数读完整 / 探针总数读一次（另带 7 条连带失败） | 9 failed |
| 夹具 | `synthetic_page` 忽略行偏移，每页记录一模一样 | `test_typical_request_normalizes_the_declared_columns`（3 条探针） | 3 failed |
| 规则 A | `page/offset` 不要求 `offset_key` | `test_paging_without_an_offset_key_is_refused` | 1 failed |
| 规则 B | 可选且无默认值的路径占位符被接受 | `test_optional_path_placeholder_without_default_is_refused` | 1 failed |
| 形状 | 指针缺失读成空结果 | `test_a_document_carrying_no_record_list_is_a_shape_failure`（4 条）+ 引擎面 2 条 | 6 failed |

还原证明：三条被临时改写的源文件在每腿结束后写回原字节，收尾再比 sha256 ——
`http_json.py 3a435a0d631925bc` / `spec.py 64375a1747944781` / `testing.py 69e1e84774673687`
全部 `IDENTICAL`，随后整组重跑 `214 passed` 且 `rc=0`。脚本自身 `rc=0`，
`legs=6 problems=0`，`VERDICT: COUNTERFACTS HOLD`。

## 过程中我自己的两处读数缺陷（留在档上，不重跑掩盖）

1. 形状那条腿我起初把 `test_missing_required_source_key_is_a_shape_failure` 也写成预期失败。
   实测没翻。原因不是套件失灵：记录里缺列由 `normalize_record` 在下一步拒绝，与指针走位无关，
   所以破坏指针不会让它变红。预期值是我推导过头，已按机制改写并整腿重跑。
2. 新套件首跑只有 `test_absent_rows_pointer_is_a_shape_failure` 一条失败，根因是我把
   「空文档」按 `rows_pointer` 正反两个方向写死了：`rows_pointer=""` 的模型其文档**就是**列表，
   `[]` 是合法的零行答案而不是形状失败。改为统一用 `{}`（有指针时缺键、无指针时不是列表），
   并把「空列表读成零行」留在对面对照格里。

## 新增的两条声明格式规则（本轮按最佳实践的改进项）

- `PaginationSpec`：`kind in {page, offset}` 必须声明 `offset_key`。缺了它，`_page_request`
  每次生成的请求一模一样，翻页只会重复第一页直到 `max_pages` 报 incomplete —— 边界不是翻页策略。
- `ModelSpec`：路径占位符必须是必填参数或带默认值的参数。`render_path` 会在取数时拒绝无值占位符，
  但作者读的是记录本身；把失败挪到声明时，350 个任务的作者不必等到第一次查询。

## 边界（这些腿没有证明什么）

- 探针不是原厂任务。`engine_driven=2/350` 没有因为本轮而改变，探针也不进入任何 AC2 的完成计数。
- 全部离线：`_http_get_json` 在模块级被换成会报错的传输，任何真实出网都会以
  `the contract suite must never send` 直接失败。SOURCE_VERIFIED 仍然一律 `verified is False`。
- CSV/ZIP/XML/Excel/SDMX 解码、POST、请求头、行级 transform、声明式 JOIN 仍不在声明格式里
  （见 `docs/evidence/C70/engine-capability-backlog.md`，若该文件已由并行腿落档）。
  cboe 的 9 个 `NOT_DECLARABLE` 不会因为本轮而减少。
- 本轮未跑 `make gate`（工作树仍在改动，A2/11 号成员留到冻结后一遍）。
