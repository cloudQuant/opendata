# C101 类型面：mypy 22 红 → 0，逐处写明每处修法的运行时依据

判定命令（仓库自己的 judge，与 `scripts/quality/ratchet.py:count_mypy` 同一 argv，`MYPY_PATHS=('opendata','alembic','alembic_data')`）：

```
export PATH="$HOME/opt/anaconda3/envs/py313/bin:$PATH"
mypy --no-color-output --no-error-summary opendata alembic alembic_data
```

读数：修复前 `rc=1 / error_lines=22`（全文 `mypy-before-22-errors.txt`）；修复后
`rc=0 / error_lines=0`（全文 `mypy-after-0-errors.txt`）。`ruff check` 对同一批 13 个文件
`All checks passed!`。下面的分类键**逐字取自** `mypy-before-22-errors.txt` 每行方括号里的
error code，不是我自己归的族名。

## 1. 22 处错误按 mypy error code 的十分法（3+5+2+2+5+4+1 = 22）

| error code | 数量 | 具体落点 | 修法 |
| --- | --- | --- | --- |
| `misc` `Cannot assign to class variable "http_transport" via instance` | 3 | `fred/models/treasury_rates.py:178`、`fred/models/fixed_rates.py:191`、`fred/models/iter2_macro.py:930` | 改**被写入的接收方** `FredSeriesFetcher.http_transport`：`ClassVar[HttpGet \| None]` → 带类级默认值 `= None` 的实例属性（引擎生成类 `http_json.py:1419` 本就是同一形态）。三处赋值语句一字未动 |
| `assignment` `expression has type "Any \| None"` | 5 | `sec/models/_common.py:206,209`、`congress_gov/models/_client.py:158,219,222` | `cast("int"/"bytes", getattr(response, ...))`；紧接下一行的运行时守卫原样保留 |
| `assignment` 同一变量在 if/else 两支被绑成不同类 | 2 | `intrinio/models/iter2_news_attributes.py:604`（`object \| Any` → `HttpResponse`）、`fred/models/_fixed_observation_pages.py:344`（`_ResponseLike` → `HttpResponse`） | else 支绑定改名 `client_response`，两支各自后续读取指向新名字 |
| `attr-defined` `"HttpResponse" has no attribute "status_code"/"content"` | 2 | `fred/models/_fixed_observation_pages.py:352,353` | 上一条改名后读取落在治理客户端响应上，加 `cast("int"/"bytes", ...)`；`type(status) is not int`:356、`type(body) is not bytes`:360 两道守卫仍在原位 |
| `no-any-return` `Returning Any from function declared to return ...` | 5 | `yfinance/models/iter2.py:174`、`yfinance/models/market_iter2.py:218`、`finviz/_source.py:83`、`federal_reserve/models/clean_rates.py:229`、`ecb/models/_client.py:71` | `cast("date"/"datetime"/"bytes", ...)`、给 `body: bytes = response.content` 加声明、把 seam 返回值先绑到标注为 `HttpResponse` 的局部。`warn_return_any` 未关 |
| `arg-type` | 4 | `intrinio/models/iter2_financials.py:327` 的 `rejected`/`missing`/`invalid` ×3、`federal_reserve/models/_primary_dealer.py:198` ×1 | 改在**调用点**：`rejected=tuple(rejected)` 等；`_primary_dealer` 用 `cast("Decimal", parse_decimal(item.value))` |
| `union-attr` `Item "bytes" of "bytes \| str" has no attribute "encode"` | 1 | `ecb/models/_client.py:58` | 补上原缺的分支 `raw.encode("utf-8") if isinstance(raw, str) else raw` |

为什么这不是"把判定放松"：`pyproject.toml` 的 `[tool.mypy]` 一个字没改
（`warn_return_any` / `strict_equality` / `disallow_untyped_defs` / `check_untyped_defs` 全在位，
也仍然**没有** `warn_unreachable`，所以 cast 后面紧跟 isinstance 守卫不会被读成 redundant cast 红），
7 处 cast 全都带一道未被删除的运行时守卫，其余修法是改名、加声明或在调用点做 `tuple()` 归一化。
`_primary_dealer` 弃用 `@overload parse_decimal` 而选 cast，是因为
`central_bank_holdings.py:473` 以裸 `bool` 传 `nullable=nullable`，加 overload 会让那处新的调用失配。

## 2. 反例臂：判据必须能咬（探针 + 面，全文见本目录两份 .txt）

### 2a. 7 处 cast 的守卫仍在位 —— 行锚定探针，非 grep 计数

`probes/cast_guard_census.py` 把站点写成 `(file, line, 期望子串, 守卫正则)` 四元组，
`guarded()` 只在该行**之后 10 个非空行**内查找守卫。面：

```
population: guard_backed_casts=7
face: guarded=7 unguarded=0
cast_census_exit=0
arm: opendata/data/providers/sec/models/_common.py:206 guard_present=True guard_deleted=False -> PASS
cast_arm_exit=0
```

反例臂是**内存里**删掉 SEC 那道守卫后重跑判定，必须读成 `unguarded=1`（`guard_deleted=False`
是"未删"的对照臂，删守卫的那一臂在同一 `--arm` 里跑并打印 PASS）。两臂都是 0 就证明不了判据能咬。

探针自身有过两次假读数，一并记账以免下次复用：v1 用 `parents[3]` 解析仓库根，落到了 `docs/`
而 FileNotFoundError；v2 报出 3 处 `UNGUARDED`，原因是 (a) 跨行的 needle 把守卫行一起吃进了
自己的匹配窗口，(b) 空行过滤发生在"取 6 行原始切片"**之后**。改成行锚定 + 10 行窗口后才是
上面的 7/7。

### 2b. `ecb/_client.py:58` 的三分支是真缺陷修复，不是为 mypy 闭嘴

`probes/ecb_seam_body_shapes.py` 用**已发布的** `seam_body` 跑 5 种载荷形态，并把 caller 搜索
限定在 `opendata/` + `scripts/`（首次草稿 `rglob` 整棵树，会去读 7.1 GB 的 C100 证据面里那些
`.py` 快照；正则也误匹配 `_seam_body(`）。面：

```
population: shapes=5 ecb_seam_body_external_caller_files=2
face: shapes_ok=5 of 5
ecb_census_exit=0
arm: old line `raw.encode('utf-8')` on bytes payload -> AttributeError('bytes' object has no attribute 'encode') PASS
ecb_arm_exit=0
```

反例臂直接把旧行 `raw.encode("utf-8")` 拿去喂 bytes 载荷：抛 `AttributeError`，即旧代码在
bytes 分支上是会炸的，`union-attr` 报的是真事。

### 2c. 5 处 `no-any-return` 与 1 处 `arg-type` cast 没有就近守卫，判据换成"这一行确实被执行过"

这 6 处不落在 2a 的 7 个站点里，所以**不能**用守卫计数为它们作证。见证方式是跑覆盖它们的测试文件，
并且用 `--cov-report=term-missing` 证明被 cast 的那一行**不在 missing 列表里**（"模块的测试通过"
不等于"这一行跑过"）。全文 `no-any-return-witness.txt`：

```
witness_run_exit=1
face: passed=282 failed=1 skipped=4 over 10 files
primary_dealer_witness: 111 passed, 0 failed (rc=0)
```

唯一那一红是 `tests/test_finviz_provider.py::test_provider_descriptor_binds_four_models_and_leaves_three_gaps_unbound`，
traceback 帧落在测试自己的集合相等断言 `test_finviz_provider.py:378`（finviz 模型名册），
不在本轮 13 个被改文件里的任何一行，属于第 3 节已经记账的两处集合相等红之一。
`_primary_dealer.py` 的覆盖率行缺失清单是 `189-190, 192, 195, 201, 203, 214`，
不含 197-198 那条 `cast("Decimal", parse_decimal(item.value))` 语句 —— 即该 cast 真被执行。

### 2d. seam 形态改动会被测试面看见

`fred/models/series.py` 的 `ClassVar`→实例属性若写错（例如漏掉 `= None` 类级默认值），
直接 `FredSeriesFetcher()` 的用例会以 `AttributeError` 而不是断言失败红掉，
`tests/test_fred_query_context.py` 的计数会把这件事暴露出来。

## 3. 归因面：被触碰模块的子集 128 红不是这批类型修复造成的

`python -m pytest -q --no-cov -p no:cacheprovider --tb=line tests/test_ecb*.py
tests/test_intrinio*.py tests/test_fred*.py tests/test_yfinance*.py tests/test_sec*.py
tests/test_congress_gov*.py tests/test_finviz*.py tests/test_federal_reserve*.py
tests/test_provider_engine*.py tests/test_engine*.py`
→ `128 failed, 16595 passed, 4 skipped in 22.19s`（全文 `subset-of-touched-modules-128-red.txt`）。

- 从 `FAILURES` 段里按 13 个被编辑文件过滤 traceback 帧：**0 帧**。
- 最大簇是 `TypeError: <locals>.fake_get() got an unexpected keyword argument 'transport'`
  （≥40 例），落点在 `opendata/data/providers/fred/models/_paged.py:118` 以
  `transport=transport` 调用 `_client._http_get(...)`，而旧测试替身只收三个位置参数。
  `_client._http_get` 自己的签名（`_client.py:94-99`）**是**接受 `transport` 的，
  所以生产路径不受影响：红在测试替身的形参，不在产品契约，也不在本轮类型修复里。
  本轮未动它——它是 wave 遗留的实现/测试管道不一致，属于 #1/#3 的套件转绿工作。
- 其余簇：36 × `FRED_BAD_QUERY`、9 × `ProviderModelQueryUpstreamError`、
  7 × `FRED_BAD_OBSERVATION`、2 × 集合相等断言、5 × congress_gov "NothingHereIsLiveVerified"
  语义红。按既定裁决，这些是**记录而不翻转**的冲突（冻结 judge vs 新架构），不在类型修复范围内。
- 曾尝试用 `git worktree add --detach /tmp/ab_head HEAD` 跑 HEAD 基线 A/B，收集期即
  `Interrupted: 4 errors during collection`——本轮 wave 的产品面大多尚未提交，HEAD 工作树
  连 provider 子集都收不起来，因此基线 A/B 不可用，归因只走"traceback 帧归属"这一条路。
  该 worktree 已 `git worktree remove` 清掉。

## 4. 顺带记录：`ruff format --check` 的两处脏不在本轮

`opendata/api/metrics.py`、`opendata/api/scripts.py` would reformat（578 文件已格式化）。
`git status --porcelain` 对这两个文件**无输出**：工作树与 HEAD 一致，脏面是已提交的，
且 `scripts/quality/ratchet.py:count_ruff` 只跑 `ruff check`（不跑 format），
因此它不是 `quality-ratchet` 的红因，本轮也不去动它。

## 5. 套件面：`--tb=line` 全量日志的可复算切分（`../suite-red-taxonomy.txt`）

`python -m pytest -q --no-cov -p no:cacheprovider --tb=line`（全量）→
`571 failed, 25706 passed, 274 skipped in 620.18s`，`SUITE_EXIT=1`，
算术核对 `571+0+25706+274=26551` 与解析到的 571 条 `FAILED` 行一致。
判定器 `probes/suite_red_taxonomy.py` 反例臂：伪造 3 红日志必须切对
（a=2/b=1），把 summary 的 `3 failed` 改成 `4 failed` 必须读成 `LOG_DISAGREES`，`arm_exit=0`。

- 按 mypy 本轮 13 个被改文件过滤 traceback 帧：`edited_file_frames=8`，且 8 帧全部是
  `_engine/http_json.py:793` 抛出的 `ProviderEngineError` 载荷，逐条读为
  4 × `FEDERAL_RESERVE_SHAPE_INVALID rejected=['beta0'…'tau2']`（Svensson 列面）、
  3 × `FEDERAL_RESERVE_SHAPE_INVALID rejected=['rate']`（`rate required=True`）、
  1 × `TRADINGECONOMICS_SHAPE_INVALID rejected=['date_span']`。
  三簇都是本轮之前就在任务清单上的已知红，与注解/改名/cast 无因果关系；
  13 个文件里的其余 12 个各 0 帧。
- 最大签名簇 `OpenBBMapError: registry model oecd/CompositeLeadingIndicator has no scenario`
  共 360 帧 = `tests/test_provider_conformance.py` 的 351 红 + 另外 9 红，
  根因是 oecd `provider.py` 里 9 条 iter2 绑定漏写 `scenario=`（判定器只报第一个 offender，
  所以 9 个缺口对外只露 1 个名字）。修复与反例臂见 `../map-scenario/scenario-census.txt`；
  该簇清零后套件红数应为 571 − 360 = 211。
