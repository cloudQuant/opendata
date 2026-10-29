# AkShare 股票日线单票试采候选

本文说明一个仅供人工启动的本地试采候选。它不注册 provider、不启用定时任务、不增加 REST API，也不写入生产数据表。当前范围固定为贵州茅台 `600519.SH`，日期 `2026-10-12` 至 `2026-10-23`，前复权/后复权均关闭（未复权）。这只是全 A 股工作中的首个单票候选，不满足全 A 股采集或覆盖验收。

## 来源与授权

适配器标识为 `akshare`，本候选实际请求东方财富（Eastmoney）的日线接口。AkShare 是调用适配层；本次候选的来源记录应写为 Eastmoney，不能仅把 AkShare 视为底层行情源。

本次可用范围依据用户声明“akshare 数据和 fuyao 数据都可以使用”。这是一项用户声明，不是本工具对供应商合同、数据授权主体、保存期限、转发权或第三方许可的认证。试采原始响应只写入操作者指定的私有本地证据目录，不提供给公共 API，不进入生产表。

运行需要一份操作者明确创建的短时本地授权 JSON。它只为固定请求提供本地 admission，不代表生产授权策略已更新。文件须由当前用户拥有、权限为 `0600`，其父目录须为当前用户拥有且权限为 `0700`；授权有效期最多 15 分钟，并且必须精确匹配本试采的股票、日期、未复权、单次请求及 64 KiB 响应上限。脚本在发送请求前，会在授权文件所在目录以独占创建方式写入该 nonce 的已消费标记。同一目录内，同一 nonce 不能重放；失败或中断也会消费该 nonce。不同 nonce 或另一目录中的副本不受此机制的全局限制，因此它不是全局重放账本。

生产的 AkShare 来源授权状态仍为 `UNKNOWN`。本地试采的显式放行不改变来源策略、生产任务或其他调用者的决策。

## 如何运行

真实试采尚未运行。只有需要执行此固定样本并准备保留本地原始证据时，操作者才应自行准备一次性授权文件和一个尚不存在的绝对证据目录。证据目录的父目录须已存在、由当前用户拥有且权限恰为 `0700`；新证据目录以 `0700` 创建，响应文件及 manifest 以私有权限独占创建。示例中的 `/private/pilot` 也须满足这些父目录条件。请勿将原始行情响应或授权文件提交到 Git、复制到共享目录或发到日志系统。

运行前将授权 JSON 的 `issued_at_utc` 和 `expires_at_utc` 填为规范 UTC 时间（六位小数并以 `Z` 结尾），并提供新的 32 位小写十六进制 `nonce`。不要复用 nonce。然后在显式 development 环境中运行：

```bash
APP_ENV=development python -m scripts.ops.akshare_stock_daily_pilot \
  --authorization-file /private/pilot/authorization.json \
  --evidence-dir /private/pilot/capture-20261028
```

脚本拒绝未显式设置的环境、`production` 和测试模式；授权 nonce 会在创建证据目录之前消耗，证据目录则在网络请求前预留。任何失败或中断后都不要复用 nonce。脚本不保证每种本地错误或崩溃都会留下 `capture.json`；保留已创建目录并检查其中实际存在的 reservation、manifest 和响应文件。当前代码只允许一次 GET，响应大小上限为 65,536 字节，不使用环境代理。

`reservation.json` 的 `state=attempt_reserved_before_transport` 表示这次请求预算和本地证据已在传输前预留，不证明请求已经发出或供应方已经收到。若只有 `reservation.json` 而没有 `capture.json`，本次结果仍未判定：不能据此标为传输前失败、传输结果未知或成功，也不能自动重试；nonce 已消费，不能重放。后续若另行尝试，须单独创建一份新的本地授权文件并使用新 nonce。

`capture.json` 保留既有的 `outcome` 字段以兼容原有记录，并新增顶层 `capture_state`，用来明确本地捕获与传输结果。当前取值含义如下：

- `sample_captured`：收到有界 HTTP 200，响应体已保存，且非空样本通过当前 wire 与请求日期窗口校验。
- `http_error_response_received`：取得 300–599 状态码的 HTTP 响应并以该响应结束。对 `HttpFetchError`，仅当客户端报告至少一次传输尝试且失败类别不是 NETWORK/TIMEOUT 时，才据状态码归入此状态。当前受治理客户端不向脚本提供错误响应体，因此这不表示错误体已保存或审查。
- `failed_before_transport`：代码能确认请求发送前已失败，例如进入请求范围前授权过期；或客户端报告 `HttpFetchError.attempts=0` 且没有 HTTP 状态码。
- `transport_outcome_unknown`：NETWORK/TIMEOUT 异常只要有非零传输尝试，即使携带已收到的 HTTP 状态码（例如响应头已到、响应体读取超时）也归此状态。没有状态码的其他异常、意外传输返回契约异常、未落在 200–599 范围的状态值，以及 `attempts=0` 但同时存在状态码的矛盾结果也归此状态。不能据此推断请求是否到达供应方，或响应是否完整。
- `response_rejected`：响应状态为 HTTP 200，但响应体边界、检索时间、wire 格式、请求窗口或非空样本校验未通过；其他 2xx 状态（例如 201/204）也会因本候选只接受精确的 200 而被拒收。

`capture_state` 对现有 `outcome` 作补充，不替代它。若进程中断或本地写入失败而没有最终 `capture.json`，上面这些捕获状态都不能从 reservation 推定。无论结果是否可判定，已消费的 nonce 均不可复用；状态未知时不要自动发起第二次请求。

成功响应必须满足 bounded-response 契约：精确的 HTTP 200、bytes 类型 body，且不超过 65,536 字节。满足该契约的响应会保存 `raw-response.bin`；后续 wire/date 校验失败的响应也会保留原始 body 并标记为拒收。违反 bounded-response 契约的响应会被拒收且不保存 body。manifest 记录请求预留信息、规范化请求描述、请求指纹、授权文件摘要、取得时间、原始响应长度和 SHA-256、解析行数及观测业务日期的最小/最大值，不包含完整日期清单。被保存的响应不等于可用或完整的行情样本。

300–599 的 HTTP 响应状态归为 `http_error_response_received`；200–299 中除精确 200 以外的状态归为 `response_rejected`，因为试采只接受精确 HTTP 200。其他无效或不在 200–599 范围的状态值归为 `transport_outcome_unknown`。当前客户端契约不会向试采脚本提供 HTTP 错误响应 body，因此 manifest 只记录可得的状态码、失败类别和“未保存 body”，不声称已保存或审查错误 body。NETWORK/TIMEOUT 且有非零传输尝试时，即使异常带有已收到的状态码（例如读取响应体期间超时），仍为 `transport_outcome_unknown`。`capture.json` 中的 `attempts_used` 是本地请求预算已预留次数，不等同于真实传输次数；客户端错误中的 `attempts` 才用于判断传输尝试，不能把预算预留数当作已发送请求数。

## 超时边界

请求设置 15 秒 overall timeout，但这是在流读取前后检查的协作式 soft deadline，不是严格总耗时上限。HTTP 库可能在产出一个迭代分块前进行多次阻塞 socket 读取；慢速 drip 响应可能显著超过 15 秒。若要给真实采集提供严格 wall-clock 上限，需单独设计和验证外部 watchdog，并确保超时终止仍保留已消耗的 nonce 和失败证据。当前候选没有该 hard watchdog。

## 离线验收记录

最终脚本经过 Ruff 等价格式修改后，隔离验收再次运行，59 项离线测试结果为 `59 passed`；脚本自身的 statement coverage 与 branch coverage 均为 `99%`，这不是整个目录的覆盖率。该轮证据早于本次 `capture_state` 字段扩展，不证明新状态字段的实现或覆盖。最终 pytest/coverage 日志已私有归档至本机 `/home/yun/Documents/opendata/var/reports/afi204/2026-10-28-pending-akshare-pilot/pytest-final.log`；早先的 `pytest.log` 仍保留为历史记录。两份日志均位于本地私有证据目录，不随本文进入仓库。

测试用 Eastmoney 形状的合成响应和 fake transport，不访问 Eastmoney、不调用 Fuyao，也不验证实际供应方当前可用性。它们验证的是本地 admission、范围固定、单次预算、私有文件、响应边界、业务日期/重复键拒收和失败处理。真实 Eastmoney 试采尚未执行，生产来源状态仍为 `UNKNOWN`。

## 完整性与使用限制

当前试采只验证非空响应能否通过严格 JSON/wire envelope 校验、响应股票代码和市场是否匹配、每条 kline 是否有 11 个字段，以及日期是否落在请求窗口内且不存在重复业务日期。脚本保留 wire 字段字符串，不校验 OHLC、成交量、成交额等数值的合理性，也不做单位标准化。它没有用独立的交易所日历判定窗口中的开市日，也没有独立核验贵州茅台的上市日期、逐日停牌状态或供应方保留边界。返回空数据、较少行数或样本日期间有空档都不能单独证明供应方漏数或数据完整。

本候选不检查头部、内部和尾部缺口，不做历史回补，不证明 2026-10-12 至 2026-10-23 的日线完整，更不证明股票上市以来的全量历史完整。报告必须保留这些未知边界；不得按固定区间行数、最新日期或一次成功响应推断历史覆盖完成。
