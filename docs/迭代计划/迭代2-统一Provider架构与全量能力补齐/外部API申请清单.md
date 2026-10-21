# 迭代 2 外部 API Key 申请清单（2026-10-21）

**范围**：台账 `模型级任务清单.csv` 中 `credential_fields` 非空的 195 行 / 15 个 provider。
**结论先行**：

- 解阻**剩余目标行**只需要 3 个 key：`biztoc`、`econdb`、`intrinio`（合计 4 行）。
- 其余 12 个 provider 的行已离线验收；它们的 key **只用于真实来源验证**（plane 3），不影响当前目标。
- `wsj`（3 行）、`stockgrid`（1 行）、`yfinance::AvailableIndices`（1 行）**不需要 key**。

> 申请入口来自参考实现的 `instructions`（REFERENCE-EXCEPTION-01：仅用于凭据/端点信息）、
> 仓内 `provider.py` 的 `environment_variable` 与各源官网。没有直达申请页的源，只写官网/文档入口，
> 不猜深链接。价格与免费额度以官网当前页面为准。

## 1. 必需：解阻剩余 4 行

| provider | 影响行 | 台账字段 | 环境变量 | 申请入口 | 说明 |
|---|---|---|---|---|---|
| `biztoc` | WorldNews（1） | `api_key` | `BIZTOC_API_KEY` | https://rapidapi.com/thma/api/biztoc | 注册/登录 RapidAPI → 订阅 Biztoc API（有免费档选免费）→ 复制 `X-RapidAPI-Key`。请求用 header |
| `econdb` | CountryProfile、ExportDestinations（2） | `api_key` | `ECONDB_API_KEY` | https://www.econdb.com （API/账户页） | 参考实现无 key 时会申请临时 token；正式 key 用于提高额度/稳定性。请求用 query `token` |
| `intrinio` | OptionsSnapshots（1） | `api_key` | `INTRINIO_API_KEY` | https://intrinio.com/starter-plan | 订阅后在账户 dashboard 取 token；Starter 计划是否收费按官网 |

**这三个拿到后**：最多 339 → 343 行；provider 覆盖 29 → 30。仍无法改变子代理 34>30 的留档。

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
| `stockgrid` | 1 | 旧端点 301 → `http://axlfi.com`（落地页，不再供数），无 key 可申请 |
| `yfinance::AvailableIndices` | 1 | 本地常量、无 HTTP 端点；受 DEC-40-02 管辖 |

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

- [ ] 必需：biztoc（RapidAPI）
- [ ] 必需：econdb
- [ ] 必需：intrinio
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
