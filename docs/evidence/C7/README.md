# C7 · 覆盖率统计扩到 `opendata_fuyao`，AC-17 的「≥90%」从「无从机算」变为逐文件实测

对应验收项：**AC-17（覆盖率子项）**。本轮不新增数据能力、不触网、不写生产仓库，
只做一件事：把 C6 轮明确记为缺口的「`opendata_fuyao` 覆盖率百分比无法机算」补掉。

## 1. 缺口原文

验收文档 AC-17 的覆盖率子项要求 `opendata/data`、`pipeline`、`opendata_fuyao`
**statement + branch ≥90%**。C6 轮 `make gate` 全绿，但归档时确认：统计范围里没有
`opendata_fuyao/` 这个包（9 个文件、790 语句 + 220 分支），所以该子项只能标注
「40+ 单测存在、百分比无从机算」（`docs/evidence/C6/README.md` §9）。

## 2. 根因：只改 `pyproject.toml` 的 `[tool.coverage.run] source` 不生效

`pytest.ini` 的 `addopts` 里带 `--cov=opendata`。pytest-cov 由**命令行**构造
`source` 列表，优先级高于 `[tool.coverage.run] source`，所以那行配置在 `make gate`
路径上是死配置。

留档症状（都是实测，不是推断）：

| 配置状态 | `make test-cov` 的 `TOTAL` 行 | `grep '^opendata_fuyao'` |
|---|---|---|
| 改前（`--cov=opendata`，pyproject `source=["opendata"]`） | `9102 1256 2118 276 84.33%` | 无匹配 |
| 只改 pyproject 的 `source` | `9102 1256 2118 276 84.33%`（**一字不差**，说明没生效） | 无匹配 |
| `pytest.ini` 增 `--cov=opendata_fuyao` | `9892 1308 2338 308 84.94%` | 9 行 |

两处配置都保留：`pytest.ini` 决定门禁实际统计范围，`pyproject.toml` 让不带 `--cov`
的裸 `python -m coverage run -m pytest` 覆盖同一范围，避免下一次再出现「配置说测了、
报告里没有」。

## 3. 实测数字（补测前 A / 补测后 B，同命令、串行不重叠、每次先 `coverage erase`）

| 文件 | A：C6 轮测试文件 | B：C7 轮 | 变化 |
|---|---|---|---|
| `dumps.py` | 88.62%（漏 16 语句 / 3 部分分支） | **96.41%**（漏 4 / 2） | +7.79pt |
| `endpoint_map.py` | 89.36%（漏 11 / 9） | **100.00%**（漏 0 / 0） | +10.64pt |
| `endpoints.py` | 91.48% | 91.48% | 不变 |
| `credentials.py` | 92.98% | 92.98% | 不变 |
| `errors.py` | 92.11% | 92.11% | 不变 |
| `http_client.py` | 93.50% | 93.50% | 不变 |
| `rate_limiter.py` | 91.04% | 91.04% | 不变 |
| `envelope.py` | 98.21% | 98.21% | 不变 |
| `__init__.py` | 100.00% | 100.00% | 不变 |
| **`opendata_fuyao` 包小计** | 91.39%（790 语句 / 220 分支，漏 87） | **94.65%**（漏 54） | +3.26pt |
| 全量 `TOTAL` | 84.94% | **85.21%** | +0.27pt |
| 用例数 | 2,361 passed / 5 skipped | **2,384 passed / 5 skipped** | +23 |

**AC-17 的 `opendata_fuyao ≥90%` 现在是逐文件可机算的事实**：9 个文件最低
`rate_limiter.py` 91.04%，全部 ≥90%。`fail_under = 84` 与 `precision = 2` 均未改动
（**没有为了通过而下调阈值**），实际总量 85.21%。

复算命令：

```bash
export PATH="$HOME/opt/anaconda3/envs/py313/bin:$PATH"
make test-cov                                                   # 全量 + 阈值判定
python -m coverage report --include='opendata_fuyao/*'          # 包小计 94.65%
python -m coverage report --include='opendata/data/providers/ths/*'   # ths 适配层
```

**一处对不上的旧数字，就地更正而不是留着**：C6 轮 §9 记的「`ths/` 层合计 90.37%」
在同一份数据上复算为 **96.84%**（213 语句 / 5 漏、40 分支 / 3 部分；四个日线 fetcher
100%、`_client.py` 97.44%、`stock_action.py` 91.30%、`registration.py` 88.24%），
把 fuyao 并进同一条 `--include` 则是 95.09%。90.37% 复现不出来，最可能是当时读的是
补测过程中的中间 `.coverage`。C6 的 README 保留原文并加了指向本节的更正备注——
口径以这里能复算出来的数字为准。

## 4. 新增 23 个单测钉住什么（不是为凑百分比写的空断言）

每个用例都断言一条**失败关闭或清理**行为，且这些分支在补测前确实没被全量套件走到。

`tests/test_fuyao_dumps.py`（+6）：

| 用例 | 钉住的行为 |
|---|---|
| `test_transport_error_is_translated_and_leaves_no_partial_file` | 流式下载中途 `httpx.ReadError` → `FuyaoError.category == "network"` **且** `dest_dir` 为空——半成品必须删掉，否则断点续传会读到坏文件 |
| `test_malformed_expiry_is_refused` | `presigned_url_expires_at: "not-a-date"` → 报 `expires_at`，不静默当成「永不过期」 |
| `test_unreadable_parquet_fails_closed` | 非 Parquet 字节 → `parquet_unreadable` |
| `test_daily_k_rejects_a_row_with_unparseable_price` | 行内 `open_price: "n/a"` → `daily_k_row`（不 `float("n/a")` 崩栈、不归零） |
| `test_adjustment_rejects_a_row_with_unparseable_number` | `dividend_per_share: "n/a"` → `adjustment_row` |
| `test_empty_adjustment_dump_fails_closed` | 列齐但零行 → `adjustment_empty` |

`tests/test_fuyao_endpoint_map.py`（+17：4 条独立 + 1 条 12 参参数化 + 1 条取值口径）：

| 用例 | 钉住的行为 |
|---|---|
| `test_planned_entry_with_method_fails_closed` | `planned`（尚未实装）条目写了 `method` 即报错，与写 `path` 同族 |
| `test_unreadable_map_fails_closed` | 映射文件路径不存在 → `is unreadable` |
| `test_map_must_declare_version_one` | `version: 2` 与缺 `version` 两种写法都拒 |
| `test_map_must_be_a_mapping` | YAML 顶层是列表时拒（不按「空映射」放行） |
| `test_malformed_rows_fail_closed`（12 参） | 缺 `sections` / `sections: []` / `entries: []` / section 行不是 mapping / 未知 family `"星际"` / 空 slug / `doc_endpoints: "many"` / entry 行不是 mapping / path 缺前导 `/` / 空 path / 缺 method / `section` 指向未知页 —— 12 种畸形行逐条报错 |
| `test_non_string_domain_is_read_as_absent` | `domain` 写成非字符串时读成「未登记」，而不是 `''.join` 崩栈 |

## 5. 门禁复跑（`make gate`，`docs/evidence/C7/gate.txt` 为完整未裁剪日志）

| 子项 | 结果 |
|---|---|
| `brand-check` / `zero-dep-check` / `js-points-check` | 通过 |
| `a2-check`（ruff + `ruff format --check` + mypy + bandit） | 通过，A2 文件 **225** 个 |
| `quality-ratchet` | `OK: quality debt did not increase.`（`ruff_selfdev 277`、`mypy_selfdev 21`、`bandit_selfdev 4`、`ruff_ported 2145`、`direct_http_ported 1045`，全部等于快照） |
| `public-api-quality` | 392 个公开 callable，docstring / 注解 **100.0%** |
| `test-cov` | `EXIT=0`、**2,384 passed / 5 skipped**、覆盖率 **85.21%**（阈值 84%） |
| 前端 `frontend-lint` / `frontend-typecheck` / `frontend-test` | 通过，**8 files / 79 tests** |
| 汇总 | `===== gate: PASSED =====` / `===== gate exit code: 0 =====` |

日志头：`branch dev`、`HEAD fa7b85b`、`dirty files 4`、`Python 3.13.5`、
`2026-09-24 21:50:35 CST`（收尾 21:53），共 **5,409 行**、未做裁剪。

一次归档前的自我更正：第一遍 `make gate` 与 §3 的对照测量在时间上重叠，两者共用
`.coverage` 数据文件存在合并污染的风险，因此那次的数字**不作为证据**。测量结束后
在无任何并发测试的状态下重跑了一遍 `make gate`，并把这一遍归档；重跑的关键行与
独立的 B 测量逐字一致（`85.21%`、`2,384 passed / 5 skipped`），说明数字可复现。

## 6. 一处已知不稳定面（本轮只观测、不改生产代码）

全量跑期间出现过一次 setup ERROR：
`tests/test_data_subscribe.py::TestSocketAuth::test_first_frame_must_be_auth`，
`-n 8` 下报
`RuntimeError: Task <...BlockingPortal._call_func...> got Future <Future pending> attached to a different loop`，
链路是 `TestClient(app)` 触发 lifespan → `opendata/core/database.py:219 create_tables`
→ 共享 async engine 的连接池里的连接归属另一个 event loop。

- 该文件单独串跑：`51 passed`。
- C7 的三次全量跑（两次 `make gate` + 一次测量）未复现。
- 记为**已知 flake 面**：修法涉及 lifespan / engine 的 loop 归属（生产代码），
  超出「扩统计范围」这一轮的范围，另轮处理。不在本轮顺手改，是为了让这轮的
  证据只承载一个变量。

## 7. 边界

- `opendata_fuyao` 入统计范围后，其网络路径仍由 `httpx.MockTransport` 覆盖，
  真机对照不在覆盖率口径内（真机证据见 `docs/evidence/C5`、`docs/evidence/C6`）。
- 未新增 fetcher / 域：注册表口径与 C6 相同（**27 能力 / 15 域 / 13 verified**、
  ths fetchers 5 个、`endpoint_map.yaml` 102 条 = implemented 11 / available 80 /
  client_only 6 / planned 5）。
- `htmlcov/`、`coverage.xml`、`.coverage` 均在 `.gitignore` 内，本轮没有把任何
  覆盖率产物提交进仓库。
