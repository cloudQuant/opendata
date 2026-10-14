# Query `symbol` 类型边界设计审查

日期：2026-10-12  
范围：只读源代码调查与类型架构评估；未改产品代码或测试，也未读 OpenBB 实现。

## 结论

`QueryParams` 目前同时承担两种互不兼容的承诺：它是所有 Fetcher query 的共同基类，又明确把 `symbol` 定义为 `str | None`。源码中找到 **10 个直接或间接继承该标量字段、随后将其拓宽为 `str | list[...]` 的 query 类**。在 strict mypy 配置下，这个继承关系不成立；用逗号串或 ignore 遮盖会丢掉实际字段类型，不能保留调用端 `.symbol`、Pydantic schema 及 `model_dump()` 所表达的值形状。

建议把类型职责拆开：`BaseQueryParams(BaseModel)` 只持有配置和 `source/market`；`QueryParams(BaseQueryParams)` 按当前顺序持有 `symbol: str | None`、`start_date`、`end_date`；`MultiSymbolQueryParams(BaseQueryParams)` 声明 `symbol: str | Sequence[str | None] | None`，随后声明日期字段。Fetcher 的 `QueryT` 上界移到 `BaseQueryParams`。各多标的子类保留当前精确字段注解、默认值、validators 和各自归一化结果，仅改其父类。各标量 query 继续继承 `QueryParams`。

这能移除类型不实的基类关系，但还不是机械替换：FMP、Deribit 有共享 query 中间类；YFinance 有一个实际 `isinstance(QueryParams)` 检查；注册 query-schema 服务也用 `issubclass(QueryParams)` 判定。按上述字段归属，scalar 和 multi 根都先声明 `symbol`、再声明日期，公共 schema / `model_dump()` 的顺序可保持。provider-specific 的多继承仍会影响新增字段顺序：FMP 的 `max_records` 必须继续出现在 symbol/date 之后。capture 的 query SHA 基于未排序 JSON 字节计算，所以应把这一顺序锁进回归。

## 源码证据与受影响 query

`opendata/data/protocol.py:56-57` 将 `QueryT` bound 到 `QueryParams`；`opendata/data/protocol.py:175-189` 定义 `QueryParams`，其中 `symbol` 是 `str | None`。`Fetcher` 在 `opendata/data/protocol.py:254` 使用该泛型，在 `:298-301` 只把 query 实例原样传给 extraction/normalization；`fetch_captured_async` 的 callback 也用同一个 `QueryT`（`:341-355`），Fetcher 核心没有把 symbol 编成字符串或消费其具体字段。

以下是对该标量字段的多类型拓宽；括号内是模型源码位置。除 FMP 两个 query 共享一个 provider 根外，共计十个拓宽点：

| Provider query | 当前继承与 `symbol` 形状 | 迁移注意事项 |
|---|---|---|
| Cboe `CboeIndexHistoricalQuery`（`opendata/data/providers/cboe/models/index_historical_public.py:64-115`） | 直接继承 `QueryParams`；`StrictStr \| list[StrictStr]`，保留原 validators 和 `.symbols` property | 切换到多标的基类；Fetcher 按 `.symbols` 逐文件取数（`:264-281`）。 |
| SEC `CompareCompanyFactsQuery`（`opendata/data/providers/sec/models/iter2_financial.py:1543-1571`） | `str \| list[str \| None] \| None`；字段 validator 会拒绝 list 中的 `None` 并保留单个 str/list 输入形状 | 保留其注解和归一化；Fetcher 的 `.symbol` 分支在 `:1614-1628`。不要把 null list 项的输入契约或结果形状“简化”为逗号串。 |
| FMP `HistoricalQuery`、`MultiSymbolQuery`（`opendata/data/providers/fmp/models/iter2_market.py:291-404`） | 两者经 `MarketQuery` 继承 `_MarketQueryBase(QueryParams)`，再将 symbol 拓宽为 `str \| list[str]` | `_MarketQueryBase` 不能继续只做 scalar `QueryParams` 子类；见下方中间类拆分。保留 `_symbol_list` 和窗口/请求数验证。 |
| IMF `ImfEconomicIndicatorQuery`（`opendata/data/providers/imf/models/iter2.py:551-600`） | 直接继承；required `str \| list[str]` | 保留 flow 同一性及 dimension validators；Fetcher 将值交给 `_selected_symbols`（`:707-714`）。 |
| Deribit `FuturesHistoricalQuery`、`FuturesInfoQuery`（`opendata/data/providers/deribit/models/fetchers.py:103-180`） | 经 `_DeribitQuery(QueryParams)` 继承；required `str \| list[str]` | 将两类移到多标的 sibling root；保留规范化及数量限制。其余 Deribit query 仍需 scalar base，因为 `_DeribitQuery` 当前提供可选 scalar symbol，且 `FuturesCurveQuery` 明确要求 scalar (`:183-228`)。 |
| Intrinio `_EtfPricePerformanceQuery`（`opendata/data/providers/intrinio/models/iter2_snapshots_clean.py:41-74`） | 直接继承；`StrictStr \| list[StrictStr]`，保留原有 1–3 ticker 及去重验证 | 多标的类切换父类；更新 Intrinio 通用 Fetcher 的 `_QueryT` bound，详见下节。 |
| YFinance `YfinanceMultiSymbolQuery`（`opendata/data/providers/yfinance/models/iter2.py:45-101`） | 直接继承；输入接受 str/list，但 validator 将模型字段归一化为逗号连接的 str，`.symbols` 再拆为 tuple | 迁移不得改变既有 provider 专属归一化；多标的 Fetcher 当前调用 `_validated_query`，该 helper 必须接受新基类。逗号编码是该 provider 已有的字段语义，不能推广为通用修复。 |
| EIA `ShortTermEnergyOutlookQuery`（`opendata/data/providers/eia/models/short_term_energy_outlook.py:104-135`） | 直接继承；`str \| list[str] \| None`；单项保持 str、多项保持 list | 原样保留可选性、去重、日期及频率校验；Fetcher 按 str/list 分支（`:170-225`）。 |

Intrinio news/attributes 的 `_CompanyNewsQuery`、`_LatestAttributesQuery`、`_HistoricalAttributesQuery` 虽也有 list symbol，但其父链是本地 `_Query(BaseModel)`，不是 `QueryParams`（`opendata/data/providers/intrinio/models/iter2_news_attributes.py:79-90,125-161,188-220`）；它们不是此次标量字段覆盖错误，不需要为了该拆分迁移。

## 类型、运行时和服务边界

1. **泛型和 provider 内部泛型。** `opendata/data/protocol.py:56-57` 的 `QueryT` 必须改 bound 到新共同根，否则任何新的 `Fetcher[MultiSymbolQueryParams, ...]` 仍不能满足协议。`opendata/data/providers/intrinio/models/iter2_market_clean.py:57,187-205` 的 `_QueryT`/`_IntrinioMarketFetcher` 也要接受共同根，因为 ETF performance query 使用该 Fetcher family；`_MarketQuery` 仍是 scalar 子类，现有 `isinstance(params, _MarketQuery)` 日期检查保持。FMP 的 `QueryT` 当前 bound 到本地 `_MarketQueryBase`（`iter2_market.py:52,291,607`），可继续 bound 到本地共享根；关键是该根要同时容纳 scalar 与 multi 分支。

2. **Provider 中间类拆分。** FMP 的 `MarketQuery` 和 `USMarketQuery` 当前都从 `_MarketQueryBase` 继承标量 symbol 默认值；它们及许多单标的/无 symbol query 的 public schema 需要保持标量字段。可把 `_MarketQueryBase` 改为共同字段根，再派生 scalar 与 multi sibling roots；`HistoricalQuery` 和 `MultiSymbolQuery` 改接 multi root，其他 leaf 仍接 scalar root。保留 `MarketQuery`/`USMarketQuery` 的 market Literal、历史日期/请求限制、normalizer；`_MarketFetcher` 及 `HistoricalQueryT` 的本地类型关系继续有明确约束。Deribit 同样应把 `_DeribitQuery` 的 config/source validator 移到不含 symbol 的共同 provider 根，再保留 scalar sibling 给 `FuturesInstrumentsQuery/FuturesCurveQuery/OptionsChainsQuery`，另为 futures history/info 建立 multi sibling。`_check_snapshot` 当前接受 `QueryParams` 并读取 `.symbol`（`fetchers.py:720-726`），应收窄为 `QueryParams | MultiSymbolQueryParams` 或定义同时声明 `symbol/start_date/end_date/market` 的只读 Protocol；不能改成无类型保证的共同根再直接取 `.symbol`。

3. **运行时 family check。** `opendata/services/provider_models.py:148-153` 从 Fetcher `transform_query` 返回注解提取实际 Pydantic schema，并以 `issubclass(query_type, QueryParams)` 作有效性门槛。迁移后 multi query 不再是 `QueryParams` 子类，因此这里必须检查 `BaseQueryParams`，否则 `GET` provider-model query-schema API（`opendata/api/provider_models.py:75-98`）对这些模型返回 schema unavailable/HTTP 500。`ProviderRegistry` 注册和 resolve 只存 `Fetcher[Any, Any]`（`opendata/data/registry.py:211-261,330-341`），没有 QueryParams family runtime guard，无需改其注册机制。

4. **YFinance 直接校验。** `opendata/data/providers/yfinance/models/iter2.py:147-154` 的 `_validated_query` 不仅标注 `type[QueryParams]`，也在运行时要求 `isinstance(params, QueryParams)`。YFinance multi-symbol Fetcher 在 `market_iter2.py:268,284,502,510,574,583,1417,1429,1626,1636` 调用它。切换 `YfinanceMultiSymbolQuery` 后这些真实 fetcher 会被拒绝，除非 helper 的 query class/返回类型与运行时检查一并泛化到共同根；最好用 `TypeVar` 保留 `query_type` 的具体返回类型。YFinance scalar statement/dividend callers (`iter2.py:263-274,347-352`) 仍兼容。

5. **query 服务与 capture。** Query service 只把 HTTP mapping 的键值送给 `fetch_async(**normalized_query)`；`_prepare_query` 只 pin source/market 并拒绝控制字段（`provider_model_query.py:125-144,236-253`），symbol str/list 不被服务序列化成逗号串。Capture seam 保留 callback 收到的具体 query object；`provider_model_raw.py:31-35,157-188,209-221` 则用精确身份 allowlist 和 `params.model_dump(mode="json")` 保存 query。当前 allowlist 仅 FredSeries、BlsSeries、FMP EquityHistorical，均为标量类；它应继续只接受这三种 exact type，不应借本次基类扩张成接受所有 multi-symbol query。`provider_model_ingest.py:475-481` 对 capture params 的 QueryParams cast 也处在该 exact allowlist 流程中；如果未来扩大 allowlist，再单独调整共同类型和显式身份绑定。

6. **字段顺序 / digest 兼容性。** 当前 `QueryParams` 声明顺序为 `source, market, symbol, start_date, end_date`。新的 common root 不应搬动日期；让 scalar/multi family 各自声明 `symbol, start_date, end_date` 可保持现顺序。隔离 Pydantic scratch 已确认两个 family 都得到相同的字段与 schema 顺序。FMP provider-common 字段 `max_records` 需要在正确 MRO 中后置；把 `FmpCommon` 放在 `QueryParams` / `MultiSymbolQueryParams` 之前时顺序为 `source, market, symbol, start_date, end_date, max_records`，反转 bases 会变成 `source, market, max_records, symbol, start_date, end_date`。由于 capture canonicalization 使用普通 `json.dumps(..., separators=...)` 且未 `sort_keys=True`（`provider_model_raw.py:102-120,157-188`），应锁住正确 MRO 和旧 capture query JSON/hash，不能静默变更 digest。

## 实现后的必要回归（本次未执行）

- 静态检查：不使用 `type: ignore`、逗号串或 `Any` 遮挡；`Fetcher` 同时接受 scalar 与 multi families；Intrinio/FMP 泛型可正确推导具体 query。仓库 mypy 使用 strict 相关选项（`pyproject.toml:282-297`），本次没有运行 mypy。
- 每个上表 10 个拓宽点做 schema/validation 对照：字段集合、requiredness、默认值和 provider 专属 symbol 联合类型不变；字符串与列表各自经过原 validator 后，`.symbol` 及 `model_dump(mode="python/json")` 与现有输出语义一致；非法列表仍在 transport 前拒绝。
- Fetcher 覆盖：Cboe `.symbols`、FMP `HistoricalQuery/SingleSeriesHistoricalQuery/MultiSymbolQuery`、IMF、Deribit、Intrinio ETF、YFinance 各自至少执行一次真实 `Fetcher.fetch`/service 路径的离线 fixture 请求，验证列表值没有在 schema/service 边界被压成字符串；EIA 单标的/多标的/无 symbol archive 三路保持原分支。
- 公共服务：`query_schema_for_model` 对 multi query 可返回 array 分支；`query_provider_model` 将 list 原样送至目标 Fetcher；错误 scalar/list 类型仍映射 query validation。不能只测 `issubclass` 或 generic parser。
- YFinance：沿 `YfinanceHistoryFetcher`、News、CurrentMultiSymbol、KeyMetrics 和 PriceTargets 的 `_validated_query` 调用路径证明新的共同根检查接受这些模型且保持 query 重验证；scalar Statement/Dividend/OptionChain/Screener 路径未回退。
- Query capture：确认现有 FredSeries/BlsSeries/FMP EquityHistorical 的 query JSON 与 digest 是保持原字节、还是按批准版本变化；若 field order 有变，需有版本/兼容说明。另确认 `fetch_captured_async` callback 对 multi query 收到具体类，并观测到与直接 `model_dump()` 相同的 symbol 类型和值。
- provider intermediate roots：验证 FMP global/US scalar schemas、historical/multi schemas和 market injection；Deribit三种 scalar snapshot schemas及两种 multi future schemas。隔离 scratch 证明 sibling composition/validator MRO 可用，产品改造仍需按真实 FMP fetcher/helper 和 Deribit snapshot 路径复测。

## 本轮读取边界

只读了仓库内 `opendata/data/protocol.py`、相关 provider query/fetcher 源码（Cboe、SEC、FMP、IMF、Deribit、Intrinio、YFinance、EIA）、`opendata/services/provider_models.py`、`provider_model_query.py`、`provider_model_ingest.py`、`opendata/api/provider_models.py`、`opendata/data/registry.py`、`opendata/pipeline/provider_model_raw.py` 和 `pyproject.toml` 的相关段落。没有读 OpenBB 实现、没有执行产品测试或 live 请求，也没有改动任何产品/测试文件；只新增本审查文档。

## Pydantic / mypy 隔离 scratch 补充

日期：2026-10-12。此补充只用 `/tmp/query_symbol_boundary_*.py` 中的合成模型，不导入或执行仓库产品模型/测试。运行环境为 Python 3.11.8、Pydantic 2.13.4、mypy 2.3.1。实际 Pydantic 校验由 Anaconda `base` 环境执行；mypy 使用同目录的 `/tmp/pydantic.pyi` 最小静态 stub（`BaseModel`、`ConfigDict`、`StrictStr`），目的是只检查模型字段继承/窄化和泛型关系，不声称检查 Pydantic 插件或其完整第三方类型实现。

执行命令：

```text
/Users/yunjinqi/opt/anaconda3/bin/conda run -n base python /tmp/query_symbol_boundary_scratch.py
/Users/yunjinqi/opt/anaconda3/bin/conda run -n base python /tmp/query_symbol_boundary_schema_compare.py
/Users/yunjinqi/opt/anaconda3/bin/conda run -n base python /tmp/query_symbol_boundary_mro_counterfact.py
/Users/yunjinqi/opt/anaconda3/bin/conda run -n base python /tmp/query_symbol_boundary_generic_provider.py
/Users/yunjinqi/opt/anaconda3/bin/conda run -n base python /tmp/query_symbol_boundary_generic_compare.py
/Users/yunjinqi/opt/anaconda3/bin/conda run -n base python /tmp/query_symbol_boundary_generic_unbound.py
/Users/yunjinqi/opt/anaconda3/bin/conda run -n base python -m mypy --config-file=/dev/null --strict --no-incremental /tmp/query_symbol_boundary_types_minimal.py
```

上述七条命令均 exit 0；mypy 输出 `Success: no issues found in 1 source file`。一次未约束 mypy 调用在 60 秒无输出后被中止，没有纳入通过证据；随后使用 stub 与隔离配置的命令通过。原始逐行输出保存在 `/tmp/query_symbol_boundary_{pydantic,schema_compare,mro_counterfact,generic_pydantic,generic_compare,generic_unbound,mypy}.log`。

`BaseQueryParams(source, market)` 加两个 sibling family 的运行时结果符合预期：scalar 和 multi 都得到 `source, market, symbol, start_date, end_date`；`MultiSymbolQueryParams.symbol = str | Sequence[str | None] | None` 将 `list[str]` 与 `list[str | None]` 都接受为序列输入。叶子仍可收窄成 `str | list[str] | None`、required `str | list[str]`、`StrictStr | list[StrictStr]` 或 SEC 的 `str | list[str | None] | None`。Pydantic 运行样例中，SEC nullable list 叶子拒绝 `["A", None]`，StrictStr list 叶子拒绝数字，Deribit common after-validator 对 scalar 和 multi 两支都拒绝非法 source；这些结果均没有改变各叶子 schema 的输入类型。

合成的旧/新模型逐项比较了 `model_fields` 顺序、完整 JSON Schema `properties` 对象和 `required` 列表（不比较不同合成类名造成的顶层 schema `title`）。root scalar、FMP scalar、FMP historical multi、Deribit scalar、Deribit multi 五组均为 `IDENTICAL`；requiredness 与默认值保留。FMP 和 Deribit 的 sibling MRO 样例分别为 `FmpCommon > QueryParams/MultiSymbolQueryParams > BaseQueryParams` 与 `DeribitCommon > QueryParams/MultiSymbolQueryParams > BaseQueryParams`，字段仍保持旧顺序；Deribit 公共 validator 确实在两个分支继承执行。

FMP 是需要固定 base 顺序的边界：`class FmpScalar(FmpCommon, QueryParams)` 与 `class FmpMulti(FmpCommon, MultiSymbolQueryParams)` 生成 `source, market, symbol, start_date, end_date, max_records`。counterfact 把 bases 倒置后字段变成 `source, market, max_records, symbol, start_date, end_date`。因此 Pydantic 多继承本身可行，但 review/test 必须锁住 provider-common base 在 query-family base 之前；倒置会改变 `model_dump()` 键顺序和基于 JSON 字节的 capture digest。Deribit common root 没有 provider 业务字段，MRO 更简单；`_check_snapshot` 仍应接 `QueryParams | MultiSymbolQueryParams` 或只读字段 Protocol。

同时验证了 `SymbolT` 参数化的 Pydantic root 和 mypy hierarchy。把 FMP/Deribit provider root 参数化后，无 sibling 多继承，specialized leaves 与合成旧模型的字段顺序、schema properties、requiredness 同样为 `IDENTICAL`；strict scratch mypy 也接受 sequence family 与 leaf narrowing。它并非无风险的替代：直接把未参数化 `GenericQueryRoot` 暴露成 query schema 时，Pydantic 2.13.4 将 `symbol` 生成成 `{}` 并接受任意对象。因此 generic root 必须保持内部/抽象用途，注册和公开 schema 只能引用参数已具体化的 leaf。若只需解除现有十个叶子的类型冲突，公共 Base + 两个 sibling family 仍更简单；FMP 可以选 sibling root，并用只读 Protocol 收束依赖 `.symbol` 的 helper，或者仅在 helper 改动过多时对 FMP 内部 provider root 加 `SymbolT`。不要把未参数化 generic root 作为中央 schema 的 query identity。

Scratch 文件/receipt SHA-256：`query_symbol_boundary_scratch.py` `afbe1e10c20eea8ce6272c3b4d1c2707e9725295970e45f53e02d8dbb42e5bc7`；`query_symbol_boundary_schema_compare.py` `4ba36481be69fbd899e7e26453cafe9db5b751f6779adead10da10ee633cb5de`；`query_symbol_boundary_mro_counterfact.py` `7d13c9f3d35afde42f3b551bfe7381f86dba0b947217224786288c5172aafc72`；`query_symbol_boundary_generic_provider.py` `7f027a84adad56f79513eeb775f88523e3c82c081ffe515103fe2271edd70db4`；`query_symbol_boundary_generic_compare.py` `16bb3b87efcad7e2935addce9dc32f1c80fc4b969b97fc01fcf977df06f265be`；`query_symbol_boundary_types_minimal.py` `9cfa548779662e505fc0a53550e01da102bae11df3604300180eae09d740a310`；`pydantic.pyi` `f3128cffab8b1230726c51eeb71ca1311dfaefd79b5501e3f873b6d9ea6c55dc`。完整 stdout 文件也在上述 `/tmp` 路径，未写入产品或测试目录。
