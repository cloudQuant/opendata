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

## Pending (network): re-run `--record`

| case | reason |
|------|--------|
| stock_daily_raw | ConnectionError: ('Connection aborted.', RemoteDisconnected('Remote end closed connection without response')) |
| stock_daily_qfq | ConnectionError: ('Connection aborted.', RemoteDisconnected('Remote end closed connection without response')) |

## A1 leftover: D10 qfq synthesis vs official em series

SKIPPED: kline fixtures not recorded (pending, see above)
