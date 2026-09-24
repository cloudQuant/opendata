# B4 / AC-13 §7 场景1 真机增量与调度注册证据（2026-09-24）

## 1. 本轮新增的生产触发面

| 组件 | 位置 | 作用 |
|------|------|------|
| 作业装配 | `opendata/pipeline/jobs.py` | 把 `PIPELINE_TEMPLATES`（`schedules.yaml`）接上 `DataPipeline`：解析 universe（`dwd_<domain>` distinct symbol，上限 5000）、按 feed 顺序跑增量、汇总 `JobResult` |
| 手动触发 | `POST /api/v1/pipeline/run` | 202 受理 + `run_id`；域无构建器时 400 快速失败（不留悬挂记录） |
| 状态回读 | `GET /api/v1/pipeline/run/{run_id}` | `running` / `succeeded` / `partial` / `failed`（含 `error` 与逐源计数） |
| 开机注册 | `opendata/main.py` lifespan → `jobs.attach_builtin_jobs()` | 仅在调度器已启动（`ENABLE_SCHEDULER=true`）时注册内置跑批 |

## 2. §7 场景1：真机日线增量（ths/fuyao 通道）

命令与完整输出见 `scene1_real_run.log`。2026-09-24 为交易日且已收盘，fuyao 通道返回当日 K。

```
jobs.run_incremental_job(domain="stock_daily", source="ths",
                         symbols=["600519","000001"], as_of=2026-09-24, resume=False)
```

实测结果（六步链路全绿，0 失败）：

| 步 | 观测 |
|----|------|
| 1 拉取 | 2 标的 × 当日窗口，`rows_written=2`，`shards_done=1` |
| 2 ods | `ods_stock_daily_ths`：`600519.SH 1237.0` / `000001.SZ 11.3`，`date_ms=1790179200000`（上海零点），`_source=ths` + 同一 `_batch_id` |
| 3 校对 | 单源运行 → 按设计跳过（双源校对需 akshare 通道，见 §4 阻塞） |
| 4 dwd | `dwd_stock_daily`：`600519` / `000001` 两行，`symbol` 为去交易所后缀的契约形态，`source=ths`、`_diff_flag=0`、`_as_of=2026-09-24` |
| 5 通知 | 水位 + `data.update` 广播钩子按批次执行（`notify` 步骤未报错，freshness 随之推进） |
| 6 断点 | 首次运行中断后重启：`resumed_shards=1`（跳过已完成分片）；本轮以 `resume=False` 复跑全链 |

新鲜度复核：`{"ods:ths": "2026-09-24", "dwd": "2026-09-24"}`（增量前 dwd 停在 2026-07-21）。

## 3. AC-13 调度注册与时间窗

`scheduler_startup.log`（`ENABLE_SCHEDULER=true` 真机启动）：

```
job id=pipeline_p0-stock-daily-incremental name=pipeline:p0-stock-daily-incremental
    trigger=cron[... day_of_week='1-5', hour='17', minute='30'] next_run=2026-09-25 17:30:00+08:00
```

即 §9.3 的 17:30 交易日窗口已作为真实 cron 作业挂载，与 `docs/evidence/B3/schedule-calibration.txt` 的 15:00 观测（当日 K 尚未返回）一致：17:30 起跑可拿到当日收盘数据。当日落地时点的多点续测仍待后续交易日补点（17:30 作业实跑由值班观察，非本迭代门槛）。

## 4. 本轮修掉的真实缺陷（均由真机/启动路径暴露）

1. **ods 落库列名口径断裂**：`ods_*` 保留源侧列名（设计 §8.1），但流水线以契约列写 ods（ths 实时通道返回契约 `Bar`）→ `OdsWriter` 缺键列失败。新增 `mapping.denormalize_frame()` + `DomainMapping.source_key`，`jobs.make_fetch_symbol()` 对契约行反投影为源侧列（含 `date_ms` 由日期按上海零点回推），`build_stock_daily_pipeline()` 的写入键改用 `source_key`。
2. **受影响键的双形态**：合并器用同一批键过滤多张 ods 表（ths 为 `600519.SH`、akshare 为 `600519`）。改为 `ods_frame_reader` 先按"窗口 ∪ 受影响日期"读，再在归一化后的契约键上匹配（`DomainMapping.to_contract_key`），与源侧拼写无关。
3. **双源合并读侧覆盖不全**：`readers` 只登记主源 → `DwdMergeService` 取副源 reader 时 `LookupError`。改为逐源登记。
4. **开机注册必崩**：`SchedulerService.add_job` 读 `job.next_run_time`（APScheduler 3.11 未启动时为 pending Job，无该属性）→ `AttributeError`，且 `cron_expression` 是包装层词表而非 `CronTrigger` 参数。改为 `_CronSink` 直接面向活调度器并构造 `CronTrigger.from_crontab(...)`（不改遗留 `opendata/services/*`，避免 A2 面扩大）。

## 5. 外部阻塞（非代码缺陷）

- `push2delay.eastmoney.com` 对本机持续 502（curl 复核同日 502）→ 场景1 的 akshare 腿与 `stock_daily_raw/qfq`、`index_daily_em`、`fund_etf_daily_em` 夹具录制无法完成。
- 上列 em 阻塞用例已在 **sina 通道补出孪生夹具**（`stock_daily_sina_raw/qfq`、`index_daily_sina`、`fund_etf_daily_sina`）：AC-6 离线回放 **12/16 PASS**，四个登记域（股票日线/指数日线/ETF 日线/日线复权）全部至少有一条字节级对照通过（见 `docs/evidence/A2/compare-report.md`）。
- 2026-09-24 18:38~19:23 本机 DNS 一度解析不了 `finance.sina.com.cn`（首轮 8 标的对照全为 ConnectionError），18:54 网络恢复后重跑一次即 PASS；结论未受影响，事故留档以免误读首轮报告。

## 6. 事故与复原（留档）

`tests/test_dwd_merge.py::TestDwdWriteAgainstMysql` 是标了 `@pytest.mark.e2e` 的**真库写入**用例，收尾 `DELETE ... WHERE symbol IN ('600519','000001')`。本轮为验证改动直接按文件运行了该用例（默认 `make gate` 已通过 `-m "not e2e"` 排除），导致这两个样本标的的 dwd 行被删除。已用 ods 双源重算复原：`merged_rows=262 / written=262`（131 行 ×2 标的，区间 2026-01-05~2026-07-21，权威 ths 优先、akshare 降级填补，`diff_flagged=235`），复核 `dwd_stock_daily` 总数 719,316 且样本计数恢复为 131/131。**待办（下轮提请拍板）**：该 e2e 用例应改用带后缀的临时表或独立测试库，避免对生产数仓做 DELETE。

## 7. 复现命令

```bash
conda activate py313
# 场景1（真机增量，写 ods/dwd，消耗 fuyao 配额）
python - <<'PY'
import asyncio
from datetime import date
from opendata.pipeline import jobs
r = asyncio.run(jobs.run_incremental_job(domain="stock_daily", source="ths",
    symbols=["600519","000001"], as_of=date(2026,9,24), resume=False))
print(r.as_dict())
PY
# 调度注册（真机启动一次即可看到 cron 作业与 next_run）
ENABLE_SCHEDULER=true python -c "..."   # 见 scheduler_startup.log 的脚本
# AC-6/B1.3 离线回放对照
python scripts/codemod/compare_with_upstream.py --compare
# 新增 sina 孪生夹具录制（需网络 + 锁 commit 的上游 checkout）
python scripts/codemod/compare_with_upstream.py --record --only stock_daily_sina_raw \
    --only stock_daily_sina_qfq --only index_daily_sina --only fund_etf_daily_sina
```

## 8. AC-11 服务端复权 vs 官方序列（跨源真机对照）

`GET /api/v1/data/equity/stock_daily?adjust=qfq|hfq` 是**服务端合成**（设计 D10：库里只有不复权价 + 因子表）。
本轮把这条断言从"同源自证"升级为**跨源对照**：我们的合成序列走 REST 查询路径本身
（`build_data_select` + `apply_adjust_to_rows`，日线取 `dwd_stock_daily`、因子取 `dwd_stock_adjust`＝同花顺因子链），
对照面取 **sina 官方复权序列**（`stock_zh_a_daily(adjust=qfq|hfq)`，另一条源、另一条因子链）。

| 指标 | 口径 | 结果 |
|------|------|------|
| 样本 | 8 标的（沪/深/科创/创业）× 131 交易日 × 2 方法 = **2,096 根** | 2026-01-05~2026-07-21 |
| qfq 水平 | `ours/official` 锚定比 ≈ 1.000（8/8 在 0.99987~1.00018） | 最大逐日偏差 **1.10e-03**（sina 价格两位小数所致的舍入量级） |
| hfq 形状 | 按中位数锚定复一后逐日偏差 ≤ **2.20e-04** | 序列形状一致；水平为每标的常数比例（sina 后复权基准与 ths 因子链锚定不同） |
| 门槛 | 相对容差 2e-3 | **PASS（0/16 失败）** |

- 工具：`scripts/ops/qfq_official_check.py`（只读数仓，网络失败逐格记 ERROR 不静默）；
  报告：`docs/evidence/B4/qfq-official-check.txt`。
- 复现：`conda activate py313 && python scripts/ops/qfq_official_check.py --symbols 600519,000001,000009,000035,000063,600036,000651,300750 --start 2026-01-05 --end 2026-07-21`
- **观测到的真实边界（非缺陷，需在文档口径内）**：`dwd_stock_adjust` 最新日期比 `dwd_stock_daily` 落后 1 个交易日，
  因此对最后一根 K 请求 `adjust=qfq` 会按 fail-closed 抛"no adjust factor"；脚本已把窗口自动收敛到因子表上限并在报告首行标注。
  收盘批次里因子构建应先于对外提供复权，或 API 侧对"缺当日因子"降级为窗口截断——留作 1C 后续项。
