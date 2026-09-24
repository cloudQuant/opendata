# 上游同步差异报告（c4f6a631c259 → fcdbf25）

- 上游仓库：https://github.com/cloudQuant/akshare.git
- 锁定基线：`c4f6a631c259783dbc2507b6b27d179b3e88079d`
- 目标提交：`fcdbf25`

## 变更清单（147 个文件）

| 状态 | 文件 |
|------|------|
| 修改 | `akshare/__init__.py` |
| 修改 | `akshare/air/air_hebei.py` |
| 修改 | `akshare/air/air_zhenqi.py` |
| 修改 | `akshare/air/sunrise_tad.py` |
| 修改 | `akshare/article/risk_rv.py` |
| 修改 | `akshare/bond/bond_buy_back_em.py` |
| 修改 | `akshare/bond/bond_china_money.py` |
| 修改 | `akshare/bond/bond_convert.py` |
| 修改 | `akshare/bond/bond_info_cm.py` |
| 修改 | `akshare/bond/bond_zh_cov.py` |
| 修改 | `akshare/bond/bond_zh_sina.py` |
| 修改 | `akshare/currency/currency.py` |
| 修改 | `akshare/economic/macro_china.py` |
| 修改 | `akshare/economic/macro_china_nbs.py` |
| 修改 | `akshare/economic/macro_other.py` |
| 修改 | `akshare/energy/energy_carbon.py` |
| 修改 | `akshare/event/migration.py` |
| 修改 | `akshare/forex/forex_em.py` |
| 修改 | `akshare/fortune/fortune_bloomberg.py` |
| 修改 | `akshare/fortune/fortune_forbes_500.py` |
| 修改 | `akshare/fortune/fortune_xincaifu_500.py` |
| 修改 | `akshare/fund/fund_amac.py` |
| 修改 | `akshare/fund/fund_em.py` |
| 修改 | `akshare/fund/fund_etf_em.py` |
| 修改 | `akshare/fund/fund_lof_em.py` |
| 修改 | `akshare/fund/fund_rank_em.py` |
| 修改 | `akshare/fund/fund_rating.py` |
| 修改 | `akshare/futures/cot.py` |
| 修改 | `akshare/futures/futures_comm_qihuo.py` |
| 修改 | `akshare/futures/futures_contract_detail.py` |
| 修改 | `akshare/futures/futures_hist_em.py` |
| 修改 | `akshare/futures/futures_hq_sina.py` |
| 修改 | `akshare/futures/futures_inventory_99.py` |
| 修改 | `akshare/futures/futures_inventory_em.py` |
| 删除 | `akshare/futures/futures_inventory_em_varieties.py` |
| 修改 | `akshare/futures/futures_roll_yield.py` |
| 修改 | `akshare/futures/futures_settlement_price_sgx.py` |
| 修改 | `akshare/futures/futures_stock_js.py` |
| 修改 | `akshare/futures/futures_to_spot.py` |
| 修改 | `akshare/futures/futures_warehouse_receipt.py` |
| 修改 | `akshare/futures/futures_zh_sina.py` |
| 修改 | `akshare/futures_derivative/futures_contract_info_cffex.py` |
| 修改 | `akshare/futures_derivative/futures_contract_info_dce.py` |
| 修改 | `akshare/futures_derivative/futures_contract_info_gfex.py` |
| 修改 | `akshare/futures_derivative/futures_spot_sys.py` |
| 修改 | `akshare/fx/currency_investing.py` |
| 修改 | `akshare/fx/fx_c_swap_cm.py` |
| 修改 | `akshare/fx/fx_quote_baidu.py` |
| 修改 | `akshare/index/index_cflp.py` |
| 修改 | `akshare/index/index_cni.py` |
| 修改 | `akshare/index/index_cons.py` |
| 修改 | `akshare/index/index_global_em.py` |
| 修改 | `akshare/index/index_global_sina.py` |
| 修改 | `akshare/index/index_kq_fz.py` |
| 修改 | `akshare/index/index_research_fund_sw.py` |
| 修改 | `akshare/index/index_research_sw.py` |
| 修改 | `akshare/index/index_stock_hk.py` |
| 修改 | `akshare/index/index_stock_zh.py` |
| 修改 | `akshare/index/index_sugar.py` |
| 修改 | `akshare/index/index_sw.py` |
| 修改 | `akshare/index/index_zh_a_scope.py` |
| 修改 | `akshare/index/index_zh_em.py` |
| 修改 | `akshare/movie/artist_yien.py` |
| 修改 | `akshare/movie/movie_yien.py` |
| 修改 | `akshare/movie/video_yien.py` |
| 修改 | `akshare/news/news_stock.py` |
| 修改 | `akshare/option/option_commodity.py` |
| 修改 | `akshare/option/option_current_szse.py` |
| 修改 | `akshare/option/option_daily_stats_sse_szse.py` |
| 修改 | `akshare/option/option_em.py` |
| 修改 | `akshare/option/option_finance.py` |
| 修改 | `akshare/option/option_finance_sina.py` |
| 修改 | `akshare/other/other_car_cpca.py` |
| 修改 | `akshare/reits/reits_basic.py` |
| 修改 | `akshare/request.py` |
| 修改 | `akshare/spot/spot_sge.py` |
| 修改 | `akshare/stock/stock_board_concept_em.py` |
| 修改 | `akshare/stock/stock_board_industry_em.py` |
| 修改 | `akshare/stock/stock_cg_equity_mortgage.py` |
| 修改 | `akshare/stock/stock_cg_lawsuit.py` |
| 修改 | `akshare/stock/stock_fund_em.py` |
| 修改 | `akshare/stock/stock_fund_hold.py` |
| 修改 | `akshare/stock/stock_hk_famous.py` |
| 修改 | `akshare/stock/stock_hk_sina.py` |
| 修改 | `akshare/stock/stock_hold_control_em.py` |
| 修改 | `akshare/stock/stock_hot_search_baidu.py` |
| 修改 | `akshare/stock/stock_industry_pe_cninfo.py` |
| 修改 | `akshare/stock/stock_industry_sw.py` |
| 修改 | `akshare/stock/stock_info.py` |
| 修改 | `akshare/stock/stock_intraday_em.py` |
| 修改 | `akshare/stock/stock_intraday_sina.py` |
| 修改 | `akshare/stock/stock_new_cninfo.py` |
| 修改 | `akshare/stock/stock_news_cx.py` |
| 修改 | `akshare/stock/stock_repurchase_em.py` |
| 修改 | `akshare/stock/stock_share_hold.py` |
| 修改 | `akshare/stock/stock_summary.py` |
| 修改 | `akshare/stock/stock_us_famous.py` |
| 修改 | `akshare/stock/stock_us_pink.py` |
| 修改 | `akshare/stock/stock_us_sina.py` |
| 修改 | `akshare/stock/stock_xq.py` |
| 删除 | `akshare/stock/stock_zh_a_tick_163.py` |
| 修改 | `akshare/stock/stock_zh_ah_tx.py` |
| 修改 | `akshare/stock/stock_zh_b_sina.py` |
| 修改 | `akshare/stock/stock_zh_comparison_em.py` |
| 修改 | `akshare/stock_feature/stock_a_below_net_asset_statistics.py` |
| 修改 | `akshare/stock_feature/stock_board_industry_ths.py` |
| 修改 | `akshare/stock_feature/stock_cyq_em.py` |
| 修改 | `akshare/stock_feature/stock_dxsyl_em.py` |
| 修改 | `akshare/stock_feature/stock_esg_sina.py` |
| 修改 | `akshare/stock_feature/stock_gdfx_em.py` |
| 修改 | `akshare/stock_feature/stock_gdhs.py` |
| 修改 | `akshare/stock_feature/stock_gdzjc_em.py` |
| 修改 | `akshare/stock_feature/stock_gpzy_em.py` |
| 修改 | `akshare/stock_feature/stock_hist_em.py` |
| 修改 | `akshare/stock_feature/stock_hist_tx.py` |
| 修改 | `akshare/stock_feature/stock_hot_xq.py` |
| 修改 | `akshare/stock_feature/stock_hsgt_em.py` |
| 修改 | `akshare/stock_feature/stock_hsgt_exchange_rate.py` |
| 修改 | `akshare/stock_feature/stock_info.py` |
| 修改 | `akshare/stock_feature/stock_jgdy_em.py` |
| 修改 | `akshare/stock_feature/stock_lhb_sina.py` |
| 修改 | `akshare/stock_feature/stock_margin_szse.py` |
| 修改 | `akshare/stock_feature/stock_qsjy_em.py` |
| 修改 | `akshare/stock_feature/stock_report_em.py` |
| 修改 | `akshare/stock_feature/stock_sns_sseinfo.py` |
| 修改 | `akshare/stock_feature/stock_technology_ths.py` |
| 修改 | `akshare/stock_feature/stock_three_report_em.py` |
| 修改 | `akshare/stock_feature/stock_zh_vote_baidu.py` |
| 修改 | `akshare/stock_feature/stock_ztb_em.py` |
| 修改 | `akshare/stock_fundamental/stock_basic_info_xq.py` |
| 修改 | `akshare/stock_fundamental/stock_notice.py` |
| 修改 | `akshare/stock_fundamental/stock_recommend.py` |
| 修改 | `akshare/utils/func.py` |
| 修改 | `akshare/utils/request.py` |
| 删除 | `tests/test_eastmoney_request_fallback.py` |
| 删除 | `tests/test_fortune_rank_export.py` |
| 删除 | `tests/test_fund_amac.py` |
| 删除 | `tests/test_fund_hk_fund_hist_em.py` |
| 删除 | `tests/test_fund_hk_rank_alias.py` |
| 删除 | `tests/test_fx_quote_baidu.py` |
| 删除 | `tests/test_hf_subscribe_exchange_symbol.py` |
| 删除 | `tests/test_spot_sge.py` |
| 删除 | `tests/test_stock_a_below_net_asset_statistics.py` |
| 删除 | `tests/test_stock_cg_lawsuit.py` |
| 删除 | `tests/test_stock_hot_search_baidu.py` |
| 删除 | `tests/test_stock_info_global_cls.py` |
| 删除 | `tests/test_xueqiu_session.py` |

## 关键函数签名差异

### `akshare/air/air_hebei.py`
- 移除：
  - `_empty_air_quality_hebei()`

### `akshare/air/air_zhenqi.py`
- 移除：
  - `_empty_air_quality_hist()`

### `akshare/air/sunrise_tad.py`
- 移除：
  - `_get_timeanddate_page(url, **kwargs)`
  - `_minutes_to_time(value)`
  - `_minutes_to_length(value)`
  - `_solar_values(day, latitude, longitude)`
  - `_fallback_sunrise_daily(date, city)`
  - `_fallback_sunrise_monthly(date, city)`

### `akshare/article/risk_rv.py`
- 移除：
  - `_empty_article_rlab_rv()`
  - `_empty_realized_volatility(name)`

### `akshare/bond/bond_buy_back_em.py`
- 移除：
  - `_request_json(urls, params)`
  - `_empty_bond_buy_back()`
  - `_empty_bond_buy_back_hist()`
  - `_format_bond_buy_back(data_json)`

### `akshare/bond/bond_china_money.py`
- 移除：
  - `_empty_bond_china_close_return()`

### `akshare/bond/bond_convert.py`
- 移除：
  - `_update_jsl_cookie(cookie)`
  - `_jsl_fetch_with_playwright(user, password)`
- 签名变更：`bond_cb_jsl` (cookie, user, password) → (cookie)

### `akshare/bond/bond_info_cm.py`
- 移除：
  - `_empty_bond_info_cm()`
  - `_empty_bond_info_detail_cm()`
  - `_post_chinamoney_json(session, url, payload, headers, retries)`

### `akshare/bond/bond_zh_cov.py`
- 移除：
  - `_request_eastmoney_json(urls, params)`
  - `_empty_cov_min()`
  - `_empty_cov_kline()`
  - `_empty_bond_cov_comparison()`

### `akshare/bond/bond_zh_sina.py`
- 移除：
  - `_empty_bond_zh_hs_spot()`
  - `_fetch_zh_bond_hs_page(payload)`

### `akshare/currency/currency.py`
- 移除：
  - `_empty_currency_rates()`
  - `_empty_currency_time_series()`
  - `_empty_currency_convert()`
  - `_get_currencyscoop_response(url, params)`

### `akshare/economic/macro_china.py`
- 移除：
  - `_empty_macro_china_urban_unemployment()`

### `akshare/energy/energy_carbon.py`
- 移除：
  - `_empty_energy_carbon_eu()`
  - `_empty_energy_carbon_sz()`
  - `_empty_energy_carbon_gz()`
  - `_empty_energy_carbon_hb()`
  - `_empty_energy_carbon_domestic()`
  - `_empty_energy_carbon_bj()`

### `akshare/forex/forex_em.py`
- 移除：
  - `_empty_forex_spot_em()`
  - `_empty_forex_hist_em()`

### `akshare/fortune/fortune_bloomberg.py`
- 移除：
  - `_empty_bloomberg_billionaires_hist_df()`

### `akshare/fortune/fortune_xincaifu_500.py`
- 移除：
  - `_empty_xincaifu_rank()`

### `akshare/fund/fund_amac.py`
- 移除：
  - `_empty_amac_fund_abs()`
  - `_empty_amac_member_info()`
  - `_empty_amac_fund_sub_info()`
  - `_empty_amac_aoin_info()`
  - `_empty_amac_member_sub_info()`
  - `_empty_amac_manager_cancelled_info()`
  - `_empty_amac_fund_account_info()`
  - `_empty_amac_fund_info()`
  - `_empty_amac_futures_info()`
  - `_enable_legacy_server_connect(context)`
  - `_post_json(url, **kwargs)`
- 签名变更：`amac_manager_classify_info` (start_page, end_page) → ()
- 签名变更：`amac_manager_info` (start_page, end_page) → ()
- 签名变更：`amac_member_info` (start_page, end_page) → ()

### `akshare/fund/fund_em.py`
- 移除：
  - `_js_to_json(js_text)`
- 签名变更：`fund_graded_fund_info_em` (symbol, max_pages) → (symbol)
- 签名变更：`fund_money_fund_info_em` (symbol, max_pages) → (symbol)

### `akshare/fund/fund_etf_em.py`
- 移除：
  - `_get_eastmoney_fund_json(url, params, endpoint_name)`

### `akshare/fund/fund_lof_em.py`
- 移除：
  - `_get_eastmoney_lof_json(url, params, endpoint_name)`

### `akshare/futures/futures_comm_qihuo.py`
- 签名变更：`_futures_comm_qihuo_process` (df, name, comm_update_time, price_update_time) → (df, name)

### `akshare/futures/futures_contract_detail.py`
- 移除：
  - `_futures_contract_detail_page_symbol(symbol)`
  - `_fallback_futures_contract_symbol_em()`

### `akshare/futures/futures_hist_em.py`
- 移除：
  - `SSLAdapter.init_poolmanager(self, *args, **kwargs)`
  - `_get_ssl_session()`

### `akshare/futures/futures_inventory_99.py`
- 移除：
  - `__fetch_99_stock_page(product_id)`

### `akshare/futures/futures_roll_yield.py`
- 移除：
  - `_empty_roll_yield_bar()`

### `akshare/futures/futures_settlement_price_sgx.py`
- 移除：
  - `__read_sgx_future_zip_date(content)`
  - `__search_sgx_future_file_num(date)`

### `akshare/futures/futures_to_spot.py`
- 移除：
  - `SSLAdapter.init_poolmanager(self, *args, **kwargs)`
  - `_get_session_with_ssl()`

### `akshare/fx/currency_investing.py`
- 移除：
  - `_empty_currency_pair_map()`

### `akshare/fx/fx_quote_baidu.py`
- 移除：
  - `_parse_foreign_rank(data_json)`
  - `_foreign_rank_params(symbol_code, page_num)`
  - `_fetch_foreign_rank_with_browser(symbol_code)`

### `akshare/index/index_cflp.py`
- 移除：
  - `_empty_cflp_df()`
  - `_format_cflp_df(data_json)`

### `akshare/index/index_cni.py`
- 移除：
  - `_empty_index_hist_cni_df()`

### `akshare/index/index_cons.py`
- 移除：
  - `_empty_index_stock_cons_weight_csindex()`
  - `_format_csindex_code(value, width)`

### `akshare/index/index_research_sw.py`
- 签名变更：`index_analysis_daily_sw` (symbol, start_date, end_date, page_size) → (symbol, start_date, end_date)

### `akshare/index/index_sw.py`
- 移除：
  - `_empty_sw_index_info(include_parent)`
  - `_format_sw_index_info(temp_df, include_parent)`
  - `_sw_index_info(level_id, include_parent)`

### `akshare/movie/artist_yien.py`
- 移除：
  - `_post_endata_artist_json(url, payload)`
  - `_endata_artist_table(data_json)`

### `akshare/movie/movie_yien.py`
- 移除：
  - `_post_endata_json(url, payload)`
  - `_endata_table(data_json, name)`

### `akshare/movie/video_yien.py`
- 移除：
  - `_empty_video_yien()`
  - `_post_endata_video_json(url, payload)`

### `akshare/option/option_em.py`
- 移除：
  - `SSLAdapter.init_poolmanager(self, *args, **kwargs)`
  - `_get_session()`

### `akshare/reits/reits_basic.py`
- 移除：
  - `_get_eastmoney_reits_json(url, params, endpoint_name)`

### `akshare/request.py`
- 签名变更：`make_request_with_retry_json` (url, params, headers, proxies, max_retries, retry_delay, timeout) → (url, params, headers, proxies, max_retries, retry_delay)

### `akshare/spot/spot_sge.py`
- 移除：
  - `_empty_spot_quotations_sge()`
  - `_empty_spot_hist_sge()`
  - `_empty_spot_benchmark_sge()`
  - `_sge_request_json(method, url, payload, request_headers, timeout, max_retries, retry_delay)`

### `akshare/stock/stock_cg_equity_mortgage.py`
- 移除：
  - `SSLAdapter.init_poolmanager(self, *args, **kwargs)`
  - `_get_ssl_session()`

### `akshare/stock/stock_cg_lawsuit.py`
- 移除：
  - `_get_file_content_ths(file)`
  - `_get_cninfo_res_code()`
  - `_lawsuit_records_to_df(records)`
  - `_post_cninfo_with_browser(url, params, accept_enckey)`

### `akshare/stock/stock_fund_em.py`
- 移除：
  - `SSLAdapter.init_poolmanager(self, *args, **kwargs)`
  - `_get_session()`
  - `_get_eastmoney_fund_flow_klines(url, params, symbol, endpoint_name)`

### `akshare/stock/stock_fund_hold.py`
- 签名变更：`stock_report_fund_hold` (symbol, date, max_pages) → (symbol, date)

### `akshare/stock/stock_hk_sina.py`
- 移除：
  - `SSLAdapter.init_poolmanager(self, *args, **kwargs)`
  - `_get_ssl_session()`

### `akshare/stock/stock_hold_control_em.py`
- 移除：
  - `SSLAdapter.init_poolmanager(self, *args, **kwargs)`
  - `_get_ssl_session()`
- 签名变更：`stock_hold_management_detail_em` (max_pages) → ()

### `akshare/stock/stock_intraday_em.py`
- 移除：
  - `SSLAdapter.init_poolmanager(self, *args, **kwargs)`
  - `_get_ssl_session()`

### `akshare/stock/stock_repurchase_em.py`
- 移除：
  - `SSLAdapter.init_poolmanager(self, *args, **kwargs)`
  - `_get_session()`

### `akshare/stock/stock_share_hold.py`
- 移除：
  - `SSLAdapter.init_poolmanager(self, *args, **kwargs)`
  - `_get_ssl_session()`
- 签名变更：`stock_share_hold_change_szse` (symbol, max_pages) → (symbol)

### `akshare/stock/stock_us_sina.py`
- 签名变更：`get_us_stock_name` (max_pages) → ()
- 签名变更：`stock_us_spot` (max_pages) → ()

### `akshare/stock/stock_zh_ah_tx.py`
- 移除：
  - `SSLAdapter.init_poolmanager(self, *args, **kwargs)`
  - `_get_ssl_session()`

### `akshare/stock/stock_zh_b_sina.py`
- 移除：
  - `SSLAdapter.init_poolmanager(self, *args, **kwargs)`
  - `_get_ssl_session()`

### `akshare/stock/stock_zh_comparison_em.py`
- 移除：
  - `SSLAdapter.init_poolmanager(self, *args, **kwargs)`
  - `_get_ssl_session()`

### `akshare/stock_feature/stock_board_industry_ths.py`
- 移除：
  - `SSLAdapter.init_poolmanager(self, *args, **kwargs)`
  - `_get_ssl_session()`

### `akshare/stock_feature/stock_dxsyl_em.py`
- 签名变更：`stock_dxsyl_em` (max_pages) → ()

### `akshare/stock_feature/stock_esg_sina.py`
- 移除：
  - `SSLAdapter.init_poolmanager(self, *args, **kwargs)`
  - `_get_ssl_session()`

### `akshare/stock_feature/stock_gdfx_em.py`
- 移除：
  - `SSLAdapter.init_poolmanager(self, *args, **kwargs)`
  - `_get_ssl_session()`
- 签名变更：`stock_gdfx_free_holding_detail_em` (date, max_pages) → (date)
- 签名变更：`stock_gdfx_holding_analyse_em` (date, max_pages) → (date)
- 签名变更：`stock_gdfx_holding_change_em` (date, max_pages) → (date)
- 签名变更：`stock_gdfx_holding_detail_em` (date, indicator, symbol, max_pages) → (date, indicator, symbol)
- 签名变更：`stock_gdfx_holding_teamwork_em` (symbol, max_pages) → (symbol)

### `akshare/stock_feature/stock_gdhs.py`
- 签名变更：`stock_zh_a_gdhs` (symbol, max_pages) → (symbol)

### `akshare/stock_feature/stock_gdzjc_em.py`
- 移除：
  - `SSLAdapter.init_poolmanager(self, *args, **kwargs)`
  - `_get_ssl_session()`
- 签名变更：`stock_ggcg_em` (symbol, max_pages) → (symbol)

### `akshare/stock_feature/stock_gpzy_em.py`
- 签名变更：`_stock_gpzy_pledge_ratio_detail_em` (filter, max_pages) → (filter)
- 签名变更：`stock_gpzy_pledge_ratio_detail_em` (max_pages) → ()

### `akshare/stock_feature/stock_hist_em.py`
- 移除：
  - `_get_eastmoney_json(url, params, endpoint_name)`

### `akshare/stock_feature/stock_hist_tx.py`
- 移除：
  - `SSLAdapter.init_poolmanager(self, *args, **kwargs)`
  - `_get_ssl_session()`

### `akshare/stock_feature/stock_hot_xq.py`
- 签名变更：`stock_hot_deal_xq` (symbol, max_pages) → (symbol)
- 签名变更：`stock_hot_tweet_xq` (symbol, max_pages) → (symbol)

### `akshare/stock_feature/stock_hsgt_em.py`
- 移除：
  - `_stock_hsgt_hold_stock_columns(indicator)`

### `akshare/stock_feature/stock_info.py`
- 移除：
  - `_cls_signed_params(params)`

### `akshare/stock_feature/stock_jgdy_em.py`
- 移除：
  - `SSLAdapter.init_poolmanager(self, *args, **kwargs)`
  - `_get_ssl_session()`
- 签名变更：`stock_jgdy_detail_em` (date, max_pages) → (date)

### `akshare/stock_feature/stock_lhb_sina.py`
- 移除：
  - `SSLAdapter.init_poolmanager(self, *args, **kwargs)`
  - `_get_ssl_session()`

### `akshare/stock_feature/stock_qsjy_em.py`
- 移除：
  - `SSLAdapter.init_poolmanager(self, *args, **kwargs)`
  - `_get_ssl_session()`

### `akshare/stock_feature/stock_report_em.py`
- 移除：
  - `SSLAdapter.init_poolmanager(self, *args, **kwargs)`
  - `_get_ssl_session()`

### `akshare/stock_feature/stock_sns_sseinfo.py`
- 移除：
  - `SSLAdapter.init_poolmanager(self, *args, **kwargs)`
  - `_get_ssl_session()`

### `akshare/stock_feature/stock_three_report_em.py`
- 移除：
  - `SSLAdapter.init_poolmanager(self, *args, **kwargs)`
  - `_get_ssl_session()`

### `akshare/stock_feature/stock_zh_vote_baidu.py`
- 移除：
  - `SSLAdapter.init_poolmanager(self, *args, **kwargs)`
  - `_get_ssl_session()`

### `akshare/stock_fundamental/stock_basic_info_xq.py`
- 移除：
  - `_get_xueqiu_session(symbol, token, timeout)`
  - `_get_xueqiu_company_data(url, params, timeout, token)`

### `akshare/stock_fundamental/stock_notice.py`
- 移除：
  - `SSLAdapter.init_poolmanager(self, *args, **kwargs)`
  - `_get_ssl_session()`

### `akshare/utils/func.py`
- 移除：
  - `SSLAdapter.init_poolmanager(self, *args, **kwargs)`
  - `_get_ssl_session()`
  - `_request_with_ssl_fallback(url, params, timeout)`

### `akshare/utils/request.py`
- 移除：
  - `_reset_eastmoney_fallback_cache()`
  - `_eastmoney_curl_interface()`
  - `_szse_curl_interface()`
  - `_szse_resolve_ips(host)`
  - `_request_hostname(url)`
  - `_is_push2his_url(url)`
  - `_eastmoney_failed_host_key(host)`
  - `_prepared_get_url(url, params)`
  - `_request_eastmoney_with_curl_interface(url, params, headers, timeout, interface)`
  - `_request_with_curl_interface_bytes(url, params, headers, timeout, interface)`
  - `_request_with_curl_resolve_bytes(url, params, headers, timeout, resolve_ip)`
  - `_request_szse_with_curl_fallbacks(url, params, headers, timeout, interface, max_retries)`
  - `eastmoney_fallback_urls(url)`
  - `request_eastmoney(url, params, headers, timeout, max_retries, **kwargs)`
  - `request_szse(url, params, headers, timeout, max_retries, **kwargs)`

## 锁校验

锁定文件的 sha256 与冻结提交完全一致。
