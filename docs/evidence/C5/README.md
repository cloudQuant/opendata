# C5 · ths 侧 P1 域第二源接入（首腿：指数日线）

对应验收项：**AC-10（1C/C1 provider 扩充）**、AC-5/AC-6（P1 域数据源与 fidelity）、
AC-7 1B（`available → implemented` 的首个推进）。

收官口径「尽量多铺 provider」在本轮落第一条腿：P1 域 `index_daily` 原本只有
akshare（em 通道，`verified=false` 故不进 `source=auto`），现在有了同花顺
（fuyao）第二源，并通过**跨 vendor 真机对照**转正。

## 1. 产物

| 文件 | 作用 |
|---|---|
| `opendata_fuyao/endpoints.py` | 新增 `INDEX_PRICES_ENDPOINT` / `build_index_prices_request` / `fetch_index_daily_bars`（无 `adjust`，≤10 年窗口自动切块） |
| `opendata/data/providers/ths/models/_client.py` | 新增 `resolve_index_code`（指数走标的清单解析，不走名称检索） |
| `opendata/data/providers/ths/models/index_daily.py` | `ThsIndexDailyFetcher`（`asset_class=index` / `verified=true`） |
| `opendata/data/mappings/ths.yaml` | `index_daily` 口径映射（`thscode`/`date_ms`/`turnover` ↔ 契约列） |
| `opendata/data/authority.json` | `index_daily: [ths, akshare]`（双源合并的权威序） |
| `opendata/data/openbb_map.yaml` | 指数条目补 ths 腿（`status=verified`） |
| `opendata_fuyao/endpoint_map.yaml` | 指数历史 K 线 `available → implemented` |
| `scripts/ops/ths_index_cross_check.py` | 真机跨 vendor 对照脚本（含离线夹具腿） |
| `ths-index-cross-check.txt` | 本轮对照报告（4 腿 × 178 交易日） |
| `tests/test_fuyao_endpoints.py`、`tests/test_ths_provider.py`、`tests/test_data_mapping.py`、`tests/test_p1_daily_fetchers.py` | 新端点/新解析器/新映射/路由转正的回归 |

## 2. 真机对照（跨 vendor，非同源自证）

```
python scripts/ops/ths_index_cross_check.py --start 2026-01-05
```

对照面：`get_registry().resolve_domain("index_daily", source="ths")`（即调用方真实路由路径）
↔ sina `stock_zh_index_daily`（另一 vendor、另一聚合链）。沪深 300 一走**录制夹具**
（`tests/fixtures/upstream/index_daily_sina`，可离线复算），其余三条走 sina 真机。

| ths 代码 | 天数 | OHLC 最大相对偏差 | 成交量最大相对偏差 | 结论 |
|---|---|---|---|---|
| `000300`（裸码→`000300.SH`，夹具） | 178 | 1.13e-06 | 3.50e-08 | PASS |
| `000001.SH` | 178 | 1.33e-06 | 9.84e-09 | PASS |
| `399001.SZ` | 178 | 3.59e-07 | 3.16e-03 | PASS（成交量见 §4 口径说明） |
| `000905.SH` | 178 | 6.84e-07 | 3.40e-08 | PASS |

交易日重叠 4/4 腿均为 **1.000**（178 天全对齐），说明两家的日历与标的口径一致；
OHLC 偏差量级 1e-6 = sina 三位小数 vs fuyao 两位小数的舍入差，不是数据分歧。

## 3. 指数代码解析为什么另开一条路

`resolve_code` 的后缀白名单是 `SH/SZ/BJ`，且依赖 `/api/meta/tickers/search`——
而检索端点对指数是**按名称**命中的（真机：`q=000300&asset_type=a-share-index` 返回空，
`q=沪深300` 才返回 `000300.SH`/`399300.SZ`/`883300.TI`）。所以指数改为：
裸码 → `/api/meta/tickers/list?asset_type=a-share-index` 全量清单（真机 **1,431** 只，
一次 `limit=10000` 即可取完）→ 精确匹配数字段，**恰好一条**才通过，否则
`THS_INDEX_SYMBOL_UNRESOLVED` 失败关闭。真机核对：该清单内**裸码零歧义**（同名不同后缀
0 例），故解析不需要猜交易所。带后缀的输入直接透传（含 `.TI` 同花顺指数/板块、`.CSI`）。

## 4. 深证成指成交量：源侧口径差异，已归档不抹平

`399001.SZ` 的成交量 ths/sina 比值区间 `1.000000 .. 1.003161`、均值 `1.000260`，
**单向 ≥1**（ths 侧恒不小于 sina）。同一字段在另三条指数上偏差是 1e-8 量级，
说明不是单位换算（手/股）错误——那类错误会同时影响所有标的且呈 100 倍台阶。
判定：两家的深证成指成交聚合范围不同（sina 未并入部分成交品种），属源侧事实，
按 AC-9 的处置口径**保留原值 + 报告留档**，不在 dwd 层抹平；价格字段仍按 5e-5
严格容差把关，成交量单列 5e-3 容差并在报告里打印比值区间，避免"放宽阈值"变成隐性放行。

## 5. 注册事实变化（复算口径）

| 口径 | 本轮前 | 本轮后 |
|---|---|---|
| 对照表条目 / `ours` 行 | 14 / 27 | 14 / **28** |
| 已启用能力 `(provider, domain)` | 24 | **25** |
| 其中 `verified` | 10 | **11** |
| 对照表覆盖数据域 | 15 | 15（`index_daily` 早已在册） |
| ths fetchers | 2 | **3**（+`index_daily`） |
| fuyao 端点映射：`implemented` / `available` | 8 / 83 | **9 / 82** |
| 全库注册数据域 | 19 | 19 |

路由行为变化：`index_daily` 从「显式 `source=akshare` 才可路由」变为
**参与 `source=auto`**（命中 ths 权威源），`tests/test_p1_daily_fetchers.py` 的
auto 拒绝用例已按域区分（其余三条 P1 域仍显式源限定）。

## 6. 边界与待办

- fuyao 指数 K 线**只支持 `interval=1d`**、单标的、无 `offset`；周/月线需本地聚合，未做。
- 指数快照（`/api/a-share-index/prices/snapshot`）、同花顺指数清单与成分股仍是 `available`：
  成分股需要新数据域与契约模型，快照需要实时链路，另轮处理。
- 后续三腿按同一流程推进：`fund_etf_daily`（上游返回**前复权**，入库前须按 D10 还原不复权）、
  `futures_daily`、`option_daily`。
- 落库链路（`ods_index_daily_ths` → `dwd_index_daily`）本轮以**离线映射回环**证明
  （`tests/test_data_mapping.py`），未向仓库真实写入指数数据；真机增量属 AC-13 触发面，
  需要写生产表，待确认后再跑。
