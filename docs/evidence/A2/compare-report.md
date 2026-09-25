# A2.5 porting fidelity comparison (AC-6)

Record-and-replay comparison: each P0 case's HTTP transcript was
recorded from the upstream checkout pinned in upstream.lock, and
replayed into the ported tree; outputs are compared with identical
columns/shape/dtypes and cell equality (float rtol=1e-09).

| case | function | rows | http calls | result |
|------|----------|------|-----------|--------|
| stock_action_dividend | `stock_history_dividend_detail` | 31 | 1 | PASS |
| stock_action_rights | `stock_history_dividend_detail` | 3 | 1 | PASS |
| financial_statement | `stock_financial_report_sina` | 103 | 1 | PASS |
| financial_indicator | `stock_financial_analysis_indicator_em` | 103 | 1 | PASS |
| index_constituent | `index_stock_cons_weight_csindex` | 300 | 1 | PASS |
| futures_daily_sina | `futures_zh_daily_sina` | 4251 | 1 | PASS |
| option_daily_sina | `option_sse_daily_sina` | 23 | 1 | PASS |
| bond_daily_sina | `bond_zh_hs_cov_daily` | 4806 | 1 | PASS |
| stock_daily_sina_raw | `stock_zh_a_daily` | 22 | 2 | PASS |
| stock_daily_sina_qfq | `stock_zh_a_daily` | 22 | 3 | PASS |
| index_daily_sina | `stock_zh_index_daily` | 6000 | 1 | PASS |
| fund_etf_daily_sina | `fund_etf_hist_sina` | 3484 | 1 | PASS |

## Pending (network): re-run `--record`

| case | reason |
|------|--------|
| stock_daily_raw | ConnectionError: HTTPSConnectionPool(host='push2delay.eastmoney.com', port=443): Max retries exceeded with url: /api/qt/stock/kline/get?fields1=f1%2Cf2%2Cf3%2Cf4%2Cf5%2Cf6&fields2=f51%2Cf52%2Cf53%2Cf54%2Cf55%2Cf56%2Cf57%2Cf58%2Cf59%2Cf60%2Cf61%2Cf116&ut=7eea3edcaed734bea9cbfc24409ed989&klt=101&fqt=0&secid=1.600519&beg=20240101&end=20240331 (Caused by NameResolutionError("<urllib3.connection.HTTPSConnection object at 0x111a1ccd0>: Failed to resolve 'push2delay.eastmoney.com' ([Errno 8] nodename nor servname provided, or not known)")) |
| stock_daily_qfq | ConnectionError: HTTPSConnectionPool(host='push2delay.eastmoney.com', port=443): Max retries exceeded with url: /api/qt/stock/kline/get?fields1=f1%2Cf2%2Cf3%2Cf4%2Cf5%2Cf6&fields2=f51%2Cf52%2Cf53%2Cf54%2Cf55%2Cf56%2Cf57%2Cf58%2Cf59%2Cf60%2Cf61%2Cf116&ut=7eea3edcaed734bea9cbfc24409ed989&klt=101&fqt=1&secid=1.600519&beg=20240101&end=20240331 (Caused by NameResolutionError("<urllib3.connection.HTTPSConnection object at 0x1116bb9d0>: Failed to resolve 'push2delay.eastmoney.com' ([Errno 8] nodename nor servname provided, or not known)")) |
| index_daily_em | EmptyReferenceFrame: upstream returned 0 rows |
| fund_etf_daily_em | RuntimeError: Eastmoney ETF history endpoint request failed: https://push2his.eastmoney.com/api/qt/stock/kline/get |

## A1 leftover: D10 qfq synthesis vs official em series

SKIPPED: em kline fixtures are not recorded from this network (em 502); the adjustment claim is checked cross-source instead by scripts/ops/qfq_official_check.py (ths factors vs official sina series)
