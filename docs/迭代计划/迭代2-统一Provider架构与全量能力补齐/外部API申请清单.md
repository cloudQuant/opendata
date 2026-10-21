# 迭代 2 外部 API Key 申请清单（2026-10-21）

**范围**：台账 `模型级任务清单.csv` 中 `credential_fields` 非空的 195 行 / 15 个 provider。
**结论先行**：

- **唯一与 key 相关的未验收行是 `biztoc`**（1 行）；key 只影响真实来源验证，离线验收可用合成 fixture。
- `econdb` 的 2 行与 `intrinio::OptionsSnapshots` **不是 key 问题**：econdb 的当前 v1 API 没有
  `CountryProfile`/`ExportDestinations` 的语义等价（`NOT_DECLARED`：单位/映射未审）；intrinio 那行需要
  engine 的 `flow.preflight_lookup + shape.columnar`（`NOT_DECLARABLE`）。
- 其余 12 个 provider 的行已离线验收；它们的 key **只用于真实来源验证**（plane 3），不影响当前目标。
- `wsj`（3 行）、`stockgrid`（1 行）、`yfinance::AvailableIndices`（1 行）**不需要 key**。

> 申请入口来自参考实现的 `instructions`（REFERENCE-EXCEPTION-01：仅用于凭据/端点信息）、
> 仓内 `provider.py` 的 `environment_variable` 与各源官网。没有直达申请页的源，只写官网/文档入口，
> 不猜深链接。价格与免费额度以官网当前页面为准。

## 1. 与未验收行直接相关：只有 biztoc（且 key 只用于 live）

| provider | 影响行 | 台账字段 | 环境变量 | 申请入口 | 说明 |
|---|---|---|---|---|---|
| `biztoc` | WorldNews（1） | `api_key` | `BIZTOC_API_KEY` | https://rapidapi.com/thma/api/biztoc | 注册/登录 RapidAPI → 订阅 Biztoc API（有免费档选免费）→ 复制 `X-RapidAPI-Key`。请求用 header；key 只用于真实来源验证 |

**离线验收**不消费真实 key。biztoc（wave43）、econdb ExportDestinations 与 yfinance AvailableIndices（wave44）、econdb CountryProfile（wave45）、intrinio OptionsSnapshots（wave46）均已离线验收（**347/350**）；key 现在只服务真实来源验证；仍无法改变子代理 34>30 的留档。

## 1b. econdb / intrinio 的 key：只服务已验收行的真实来源验证

| provider | 未验收行 | 为什么 key 解不了 | 申请入口 |
|---|---|---|---|
| `econdb` | 无未验收行 | ExportDestinations（wave44）与 CountryProfile（wave45）均已用无凭证公开路由落地，key 不改变任何判定 | https://www.econdb.com |
| `intrinio` | 无未验收行 | OptionsSnapshots 已于 wave46 用两次请求的手写 fetcher 落地（38/38），key 只服务真实来源验证 | https://intrinio.com/starter-plan |

## 2. 可选：真实来源验证（12 个已离线验收的 provider）

| provider | 行数 | 字段 | 环境变量 | 申请入口 | 说明 |
|---|---|---|---|---|---|
| `alpha_vantage` | 3 | `api_key` | `ALPHA_VANTAGE_API_KEY` | https://www.alphavantage.co/support/#api-key | 免费，填表+验证码 |
| `benzinga` | 4 | `api_key` | `BENZINGA_API_KEY` | https://www.benzinga.com ；文档 https://docs.benzinga.io/ | 商业 API，需申请/订阅；请求用 query `token` |
| `bls` | 2 | `api_key` | `BLS_API_KEY` | https://data.bls.gov/registrationEngine/ | 免费注册 |
| `cftc` | 2 | `app_token` | `CFTC_APP_TOKEN`（建议命名） | https://evergreen.data.socrata.com/signup | 无 token 也可用但会被限流；生成 app_token |
| `congress_gov` | 8 | `api_key` | `CONGRESS_GOV_API_KEY` | https://api.congress.gov/sign-up/ | 免费，key 邮件发送 |
| `eia` | 2 | `api_key` | `EIA_API_KEY` | https://www.eia.gov/opendata/register.php | 免费 |
| `fmp` | 69 | `api_key` | `FMP_API_KEY` | https://site.financialmodelingprep.com/developer/docs | 免费档足够离线/轻量验证 |
| `fred` | 36 | `api_key` | `FRED_API_KEY` | https://fred.stlouisfed.org （My Account → API Keys） | 免费 |
| `nasdaq` | 9 | `api_key` | `NASDAQ_DATA_LINK_API_KEY` | https://data.nasdaq.com | 原 Quandl，免费账号可申请 |
| `tiingo` | 7 | `token` | `TIINGO_TOKEN` | https://www.tiingo.com （Account → API） | 有免费档；请求用 query `token` |
| `tradier` | 5 | `api_key;account_type` | `TRADIER_TOKEN`（+ account type） | https://documentation.tradier.com ；API 设置 https://dash.tradier.com/login?redirect=settings.api | 需开户；可用 sandbox。`account_type` 记 sandbox/live |
| `tradingeconomics` | 1 | `api_key` | `TRADINGECONOMICS_API_KEY` | https://tradingeconomics.com ；文档 https://docs.tradingeconomics.com/ | 商业 API，按套餐；请求用 query `c` |

## 3. 明确不需要 key

| provider | 行 | 原因 |
|---|---|---|
| `wsj` | 3 | 公开 JSON 端点；默认 UA 403，浏览器 UA 200（已实测 25,118–25,336 B）。实现待 `/goal resume` |
| `stockgrid` | 1 | 旧端点重定向后 **200 / 1,261,465 B / final=`http://axlfi.com/landing` / 非 JSON**（2026-10-22 `w150` 复测；同一次运行的 dead-host 反例给 0 B + URLError，说明这个体量是服务端给的，不是本机路由问题）。落地页不是数据源：**要的是 AXLFI 的数据端点与条款**，无 key 可申请 |
| `yfinance::AvailableIndices` | 1 | **已于 wave44 离线落地**（本地参考表 + 伪 transport 见证） |

其余 provider（cboe、finviz、imf、oecd、tmx 等）台账 `credential_fields` 为空，本清单不涉及。

## 4. 交接方式（安全）

1. **不要把 key 贴进聊天或提交到仓库。** 放到仓库外的环境变量，或 `~/.config/opendata/keys.env`
   （`chmod 600`，确认不在任何 git 仓库内）。
2. 只告诉我**变量名/文件路径**，以及一句书面授权：「允许对 <provider> 做带凭据的公开数据 GET」。
   我运行时读取，不打印、不写入日志、不提交；提交前的 staged 凭据扫描会再次拦截。
3. 优先顺序：先 `BIZTOC_API_KEY`、`ECONDB_API_KEY`、`INTRINIO_API_KEY`（解阻目标行）；
   Section 2 的 key 可一次注册、以后做真实来源验证时再交给我。
4. 若某个源注册时要求填「回调地址/应用名」等，先用占位（如 `opendata-local`），不影响 key 使用。

## 5. 一次性注册勾选清单

- [ ] 与未验收行相关：biztoc（RapidAPI；key 仅用于 live）
- [ ] live 可选：econdb
- [ ] live 可选：intrinio
- [ ] 可选：alpha_vantage（免费）
- [ ] 可选：bls（免费）
- [ ] 可选：congress_gov（免费）
- [ ] 可选：eia（免费）
- [ ] 可选：fred（免费）
- [ ] 可选：nasdaq（免费账号）
- [ ] 可选：tiingo（免费档）
- [ ] 可选：fmp（免费档）
- [ ] 可选：benzinga（商业）
- [ ] 可选：tradier（开户/sandbox）
- [ ] 可选：tradingeconomics（商业）
- [ ] 可选：cftc（Socrata app_token）

**注意**：Section 2 的 key 不会改变当前目标的三个未达成原因（残差行、3 个无行 provider、子代理越界）；
它们只服务于「真实来源验证」，那是与离线验收分开记账的另一条平面。
