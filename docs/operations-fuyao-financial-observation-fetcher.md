# Fuyao 财报观察抓取 helper 运维说明

本文描述 OpenData 中待合并的 `ThsFinancialObservationFetcher` 当前边界。它是一项需要调用方显式装配并调用的受限能力，不代表财报数据已进入生产服务。

## 当前能力与启用条件

调用方必须显式注入 Fuyao 凭证、文件型 SQLite `SourceAttemptGate`、经审核的标的注册表，以及利润表、资产负债表、现金流量表三类单位 profile 声明。每次实际请求所选报表前，还会验证该 profile 的科目覆盖完整且摘要一致。凭证以对象注入；helper 不读取项目 `.env`，也不新增凭证发现或管理入口。凭证不得写入日志、测试输出或证据归档。

请求还必须处于调用方创建的执行 scope 中，并带有与 THS 财报查询绑定的 source-attempt 授权。这里的“可信”依赖受信任进程遵守调用约定，不构成本机任意代码或操作者之间的隔离边界。标的必须是注册表中精确解析的单个规范 A 股代码。每次物理发送都经过 attempt gate，并在受控 SQLite gate 中持久记录发送预留和结果；传输最多尝试三次，调用方的授权预算必须覆盖允许的物理发送次数。gate 或 scope 缺失、失效、绑定不符或预算不足时应拒绝请求。

单次调用只请求一张报表和一种周期：利润表、资产负债表、现金流量表之一，配合 `annual` 或 `quarterly`。范围必须是最长十年的闭区间，或 `limit` 为 1 至 20；不得同时指定窗口和 limit。该 helper 不执行全市场枚举。

## 不会自动运行或持久化财报数据

helper 未注册到 Provider registry，也没有 REST 路由、调度任务或默认启用开关。它不把财报观察值或响应体写入归档、缓存、warehouse 或其他数据存储；SQLite attempt gate 中的发送预留和结果审计记录会持久化。成功结果及响应字节只在返回对象的内存中；这里的字节是 HTTPX 已缓冲并按内容编码解压的 entity body，不是压缩后的 wire bytes。调用方结束进程后，helper 本身不保留财报观察值或响应体。需要持久化财报数据或接入调度时，必须另行设计、审核并验收相应链路。

## 时间、历史覆盖与单位口径

结果中的 `observed_at` 是 OpenData 进程收到响应后采样并归一为 UTC 的观察时刻。它不等于报告披露时刻，也不能证明点时数据正确、历史修订已恢复或历史归档完整。结果的 `coverage_assessment` 固定为 `not_assessed`；返回行数或当前观察到的期间不能用于推断完整性。全 A 股覆盖、上市以来全历史、缺口修复和修订版本回放均未验收。

[Fuyao 财务报表公开文档](https://fuyao.aicubes.cn/docs/api-reference/financials/) 的本地公开契约摘录记录：时间戳按 Asia/Shanghai 解释；金额字段为原币元，`basic_eps` 为元/股，A 股币种为 CNY；`period_end_ms` 表示报告期末，`report_date_ms` 表示披露日。该文档没有进一步说明 `report_date_ms` 对首发与修订的区分或盘中精确时刻。

helper 使用调用方提供的原生单位 profile，并检查声明、映射摘要和科目覆盖是否一致。摘要只用于检测声明映射是否漂移；`operator_declared` 证据类型及引用路径都不证明每个科目的单位映射已经独立核验。单位仍未逐科目完成业务验收。

官方契约也没有说明季度利润表和现金流量金额采用单季值还是年初至今累计值。该语义未解决前，不得将相关字段用于财务因子、策略回测或其他需要季度可比性的业务计算；不得自行差分或补造单季数值。

## 已有验证及其边界

候选 helper 的隔离合成回归记录为 **147 passed**；目标模块 statement 与 branch 合并覆盖率为 **99%**；`ruff check` 和 `ruff format` 已通过。测试使用合成响应与离线传输，证明的是限定输入下的代码行为，不证明供应商数据语义或生产运行。回归文件、测试文件及已归档 pytest/coverage 输出的 SHA-256 如下，便于核对它们对应的候选版本：

`var/reports/afi204/2026-10-28-fuyao-fetcher-regression/`

| 对象 | SHA-256 |
| --- | --- |
| `opendata/data/providers/ths/models/financial_observation_fetcher.py` | `9520f64f24feb6c44daca20e55ef28fec8788ebb508915b4fcd7fb284560775a` |
| `tests/test_ths_financial_observation_fetcher.py` | `0ed5d7fafc3f88a8c377e596b608d35e96dc327c611380ca163141a931916baf` |
| `pytest.log` | `6c98da27c7bd4ef2d0557ac03e773599813273187ab2e2a55ec9cfeaef8bd2ac` |
| `coverage.log` | `080af868917214b8044f0cbbd55b136c862cc473f9b35e1aaaee8ccb1f65467b` |

上述文件哈希只绑定内容，不是数字签名。`ruff check` 和 `ruff format` 的本次状态为通过；此目录当前归档了 pytest 与覆盖率日志。

另有 `600519.SH` 六种报表与周期组合的本地 smoke，使用六个真实样本经 HTTPX `MockTransport` 回放、合成且未验证的单位声明以及每例临时 SQLite gate。一个 pytest 测试项依次覆盖全部六个组合，因此结果为 **1 passed**。该 smoke 仅核对受控本地组合；它不覆盖归档、调度、生产 API、业务单位真实性、季度累计语义或历史完整性。其脱敏摘要位于：

`var/reports/afi204/2026-10-28-fuyao-six-fixture-smoke/RUN_SUMMARY.md`

| 对象 | SHA-256 |
| --- | --- |
| `RUN_SUMMARY.md` | `b9c3957b30941f3e9cd792af98edfb9e9cb3dcfcf79edded4d86a0b19b274033` |
| `pytest.log` | `172896570462eb33dfe37e808a819a44f06f79fc7b2e06eb2074dd578202a8aa` |
| `test_fuyao_six_capture_diagnostic.py` | `adf1aaa4e939e706a94c2fd7dc670240d02a297d9a33e4c3d2217e5369b333ae` |

真实财报响应和 API 凭证不应进入本文、Git 跟踪文件或公开测试日志。六个样本不能替代持续采集，也不能证明全 A 股或历史数据完整。

## 上线前仍需完成

生产使用前，仍需独立确认受控凭证注入方式、单位映射及季度口径，设计经审查的归档和修订保留方案，并完成 Provider/API/调度接线、安全权限、监控告警、重试预算及真实数据验收。上述工作完成前，该 helper 保持未注册、未调度和默认不启用状态。
