# Eastmoney 沪深股票日线 source-native 合约 v1

**状态：** 本地候选合约；未通过真实 Eastmoney 响应验收。

**适用对象：** <code>opendata.data.providers.akshare.stock_daily_native</code>。
**兼容边界：** 本合约不修改既有 <code>Bar</code>、AkShare v1 映射、旧 wire、数据库、配置或调度。

## 身份与证据边界

本仓代码将 adapter 标识为 <code>akshare</code>，配置的底层产品标识为
<code>eastmoney.push2his.stock.kline</code>，请求目标为
<code>https://push2his.eastmoney.com/api/qt/stock/kline/get</code>。adapter 和产品必须分开记录：
AkShare 是仓内调用适配层，URL 指向的产品是 Eastmoney。合约中的产品身份来自本地代码，
不是供应方签名或真实网络响应的证明。

真实请求/响应、供应商授权、保存权、转发权、单位、复权语义和历史保留边界均未在本次
验收。项目中的 wire 形状与合成测试只证明本地解析器遵循这个仓内候选合约。它们不证明
Eastmoney 当前服务接受请求、返回预期字段或为该用途提供授权。运行时来源授权仍应由独立
策略决定；本合约固定输出 <code>source_authorization_status=unknown</code>。

## 范围和请求

每个请求必须指定单一证券和闭区间业务日期。代码路由与当前仓内 wire 一致：六位代码以
<code>6</code> 开头走沪市 ID <code>1</code>；以 <code>0</code> 或 <code>3</code> 开头走深市 ID
<code>0</code>。显式 <code>SH</code>/<code>SZ</code> 后缀必须与代码路由一致。北交所后缀和本地北交所代码前缀一律失败关闭。该前缀路由不等于已验证的完整
沪深股票名录，也不证明所有符合此前缀的证券都是当前有效 A 股。

请求固定为日线 <code>klt=101</code>、<code>fqt=0</code>，字段列表
<code>f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61</code>。开始和结束日期都显式传入，格式为
<code>YYYYMMDD</code>。每次请求最多覆盖本地 wire 的 3,660 个日历日，响应体最多 2 MiB，最多
10,000 条 kline。<code>fqt=0</code> 只记录本地请求参数；OHLC 的“未复权”解释仍标为
<code>unverified</code>，不把请求参数提升为经供应方确认的语义。

本地 request-summary 哈希只覆盖规范化的 <code>source</code>、<code>market</code>、
<code>symbol</code>、日期及空的 <code>adjust</code> 参数。它不覆盖 HTTP 方法、URL、query string、
headers、传输过程或供应方收到的请求，因此不是发送或接收凭证。raw-evidence manifest 中的
<code>http_status</code> 和 request-summary 哈希也来自调用方声明/本地计算；虽然 reader 会校验这些值
与本地 manifest 的内容寻址引用一致，并要求声明状态为 HTTP 200，但这不证明真实传输曾返回
该状态，也不证明摘要对应实际发出的请求或供应方收到的请求。

## 行格式及原始值

仓内 parser 期望响应 envelope 中的 <code>rc</code> 为整数零，<code>data.code</code> 和
<code>data.market</code> 与请求路由一致，<code>data.klines</code> 为字符串列表。每条 kline 必须有
11 个逗号分隔 token，顺序按当前仓内字段映射如下。表中的映射是本地解析代码约定，尚未由
本次真实源响应独立验证。

| 序号 | wire 字段 | 合约字段 | 保存方式 | 单位/业务语义状态 |
|---:|---|---|---|---|
| 1 | <code>f51</code> | <code>trade_date</code> | 原 token 和完整 source line 保留；另解析为业务日期用于范围检查 | 日期解释由本地 wire 合约指定 |
| 2 | <code>f52</code> | <code>open</code> | 原文本 token | 单位未知；价格复权语义未验证 |
| 3 | <code>f53</code> | <code>close</code> | 原文本 token | 单位未知；价格复权语义未验证 |
| 4 | <code>f54</code> | <code>high</code> | 原文本 token | 单位未知；价格复权语义未验证 |
| 5 | <code>f55</code> | <code>low</code> | 原文本 token | 单位未知；价格复权语义未验证 |
| 6 | <code>f56</code> | <code>volume</code> | 原文本 token | 单位未知；不乘 100，不声称是手或股 |
| 7 | <code>f57</code> | <code>amount</code> | 原文本 token | 单位未知；不声称是人民币元 |
| 8 | <code>f58</code> | <code>amplitude</code> | 原文本 token | 单位与计算口径未知 |
| 9 | <code>f59</code> | <code>change_percent</code> | 原文本 token | 单位与计算口径未知 |
| 10 | <code>f60</code> | <code>change</code> | 原文本 token | 单位未知 |
| 11 | <code>f61</code> | <code>turnover_rate</code> | 原文本 token | 单位与计算口径未知 |

本合约不把任何数值字段转为 <code>float</code>、不做量纲换算，也不把 source-native row 映射到 v1
<code>Bar</code>。原文本 token <code>"0"</code> 与空 token <code>""</code> 保持不同；字面
<code>"NULL"</code> 也只作为文本保留。当前没有经过真实响应审查的空值标记表，所以解析器不宣称
空 token 或其他 sentinel 已具有数据库 NULL 语义。字段数不足或超出 11 个时拒绝解析；原始
响应应由上游 evidence writer 保留以供调查。重复业务日期不会被删除，而是保留所有行并在
<code>duplicate_trade_dates</code> 标出。

## 日期、观测和保存时间

- <code>trade_date</code> 是 kline 中的业务日期，不带时区。
- <code>observed_at_utc</code> 是调用者声明的响应取得时间，不是供应方发布时刻。值必须是 UTC-aware
  <code>datetime</code>，状态始终为 <code>caller_declared_unverified</code>；body-only 调用不提供时则为
  <code>unavailable</code>。
- <code>captured_at_utc</code> 从已有 raw-evidence manifest 读取，是本地 evidence writer 的时间。
  其 manifest 字节会纳入 SHA-256 校验，但这不验证系统时钟准确性。当前 manifest 不包含
  <code>observed_at_utc</code>，所以两个时间由调用者并列提供，不被同一 manifest 原子绑定。
- 不生成 <code>source_published_at</code>、财报式 PIT 时间或其他源端时间字段。

<code>parse_eastmoney_stock_daily_response</code> 接受调用者提供的 bounded bytes，计算本地 body hash；
结果不声称字节来自 Eastmoney，也不声称已持久化。<code>parse_verified_stock_daily_capture</code> 延迟调用
当前股票作业中的 <code>_verified_capture</code>，复用其内容寻址 artifact 检查：重新校验本地 manifest、
私有文件路径和权限、manifest 中的 HTTP 200 状态、响应体长度与 SHA-256、manifest 规范 JSON
与 SHA-256，并比对当前请求的
request-summary 哈希及固定 <code>provider_id=akshare</code>、<code>operation_id=stock-daily-wire</code> 声明。

该 reader 的通过状态只表示本地 body/manifest 与其内容寻址引用一致。manifest 中的
<code>http_status</code>、<code>request_summary_sha256</code>、<code>provider_id</code>、
<code>dataset_id</code>、<code>operation_id</code>、<code>retention_class</code>、
<code>source_history_limit</code> 都由调用者提供；它们不是供应方身份证明、授权凭据或保留期限证据。
dataset ID 只作为声明值保留。新合约会把来源身份、保留信息和 HTTP 状态来源标成
<code>caller_declared_unverified</code>，不提供让调用者设置为“source verified”的参数。该调用目前依赖
<code>stock_daily_job._verified_capture</code> 私有接口；若要长期复用，应另行把 reader 提升为明确的公共 API。

## 覆盖状态与缺失语义

合约仅报告 <code>request.start_date/end_date</code>、实际行的最小/最大业务日期和原始行列表。空行响应
的 observed bounds 为 null。任何情况下 <code>history_coverage_status</code>、
<code>missing_date_interpretation</code> 均保持 <code>unknown</code>：

- 单个区间返回为空或少行不能证明上市以来历史完整或数据缺失。
- 一行缺席不等于停牌；需要独立交易日历、逐证券挂牌区间和停牌证据才能分类。
- 最早/最晚返回日期不等于上市日、供应商保留起点或当前最新可用日。
- 不补造缺失日线，不从零成交量、零成交额或缺少一行推断停牌。
- 全历史验收必须针对每只股票及来源业务键分别核对头部、内部和尾部；本合约不做该
  验收，也不宣称支持全 A 股名录或覆盖全历史。

## 合成测试范围与后续验收

<code>tests/test_stock_daily_native.py</code> 使用合成 Eastmoney-shaped JSON 和本地 raw-evidence writer，
覆盖字段 token 原样保留、空值与零的区别、沪深路由、北交所拒绝、响应身份与日期窗校验、
重复日期保留、证据 hash/请求摘要绑定及 tamper 拒绝。这些测试不访问网络，不包含真实源
响应，所以不构成供应方验收。

要把合约提升为 source-verified，需先在有效来源策略允许的范围内捕获真实原始响应，保存
私有证据与请求/响应 hash，再由独立审查确认每个字段的单位、NULL 表示、<code>fqt=0</code> 价格语义、
沪深 market ID、证券范围和历史保留边界。授权、持久保存与内部 REST 转发权限须独立确认；
完成之前，这些状态一律保持未知。后续标准化合约应作为独立版本引入，不能以本模型或旧
<code>akshare.yaml</code> 的 <code>volume scale: 100</code> 声明替代该证据。
