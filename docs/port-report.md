# 搬运差异报告（A1.5/A1.6，FR-5）

- 上游基线：`https://github.com/cloudQuant/akshare.git` @ `c4f6a631c259783dbc2507b6b27d179b3e88079d`
- 已搬运文件：132（含资源）
- 人工待办：**0**（A1.6 验收要求为 0）

## 逐文件对照（改写计数与重放一致性）

| 文件 | 上游路径 | import 改写 | 字符串改写 | 人工改动 | 重放一致 |
|------|---------|------------|-----------|---------|---------|
| `__init__.py` | `akshare/__init__.py` | 110 | 0 | False | ✓ |
| `_version.py` | `akshare/_version.py` | 0 | 0 | False | ✓ |
| `datasets.py` | `akshare/datasets.py` | 0 | 2 | True | ✓ |
| `exceptions.py` | `akshare/exceptions.py` | 0 | 0 | False | ✓ |
| `file_fold/__init__.py` | `akshare/file_fold/__init__.py` | 0 | 0 | False | ✓ |
| `file_fold/calendar.json` | `akshare/file_fold/calendar.json` | 0 | 0 | False | ✓ |
| `index/__init__.py` | `akshare/index/__init__.py` | 0 | 0 | False | ✓ |
| `index/cons.py` | `akshare/index/cons.py` | 0 | 0 | False | ✓ |
| `index/index_cflp.py` | `akshare/index/index_cflp.py` | 0 | 0 | False | ✓ |
| `index/index_cni.py` | `akshare/index/index_cni.py` | 0 | 0 | False | ✓ |
| `index/index_cons.py` | `akshare/index/index_cons.py` | 1 | 0 | False | ✓ |
| `index/index_csindex.py` | `akshare/index/index_csindex.py` | 0 | 0 | False | ✓ |
| `index/index_cx.py` | `akshare/index/index_cx.py` | 0 | 0 | False | ✓ |
| `index/index_drewry.py` | `akshare/index/index_drewry.py` | 1 | 0 | False | ✓ |
| `index/index_eri.py` | `akshare/index/index_eri.py` | 0 | 0 | False | ✓ |
| `index/index_global_em.py` | `akshare/index/index_global_em.py` | 1 | 0 | False | ✓ |
| `index/index_global_sina.py` | `akshare/index/index_global_sina.py` | 1 | 0 | False | ✓ |
| `index/index_hog.py` | `akshare/index/index_hog.py` | 0 | 0 | False | ✓ |
| `index/index_kq_fz.py` | `akshare/index/index_kq_fz.py` | 0 | 0 | False | ✓ |
| `index/index_kq_ss.py` | `akshare/index/index_kq_ss.py` | 0 | 0 | False | ✓ |
| `index/index_option_qvix.py` | `akshare/index/index_option_qvix.py` | 0 | 0 | False | ✓ |
| `index/index_research_fund_sw.py` | `akshare/index/index_research_fund_sw.py` | 1 | 0 | False | ✓ |
| `index/index_research_sw.py` | `akshare/index/index_research_sw.py` | 1 | 0 | False | ✓ |
| `index/index_spot.py` | `akshare/index/index_spot.py` | 0 | 0 | False | ✓ |
| `index/index_stock_hk.py` | `akshare/index/index_stock_hk.py` | 2 | 0 | False | ✓ |
| `index/index_stock_us_sina.py` | `akshare/index/index_stock_us_sina.py` | 1 | 0 | False | ✓ |
| `index/index_stock_zh.py` | `akshare/index/index_stock_zh.py` | 5 | 0 | False | ✓ |
| `index/index_stock_zh_csindex.py` | `akshare/index/index_stock_zh_csindex.py` | 0 | 0 | False | ✓ |
| `index/index_sugar.py` | `akshare/index/index_sugar.py` | 0 | 0 | False | ✓ |
| `index/index_sw.py` | `akshare/index/index_sw.py` | 1 | 0 | False | ✓ |
| `index/index_yw.py` | `akshare/index/index_yw.py` | 0 | 0 | False | ✓ |
| `index/index_zh_a_scope.py` | `akshare/index/index_zh_a_scope.py` | 0 | 0 | False | ✓ |
| `index/index_zh_em.py` | `akshare/index/index_zh_em.py` | 1 | 0 | False | ✓ |
| `pro/__init__.py` | `akshare/pro/__init__.py` | 0 | 0 | False | ✓ |
| `pro/client.py` | `akshare/pro/client.py` | 0 | 0 | False | ✓ |
| `pro/cons.py` | `akshare/pro/cons.py` | 0 | 0 | False | ✓ |
| `pro/data_pro.py` | `akshare/pro/data_pro.py` | 2 | 0 | False | ✓ |
| `request.py` | `akshare/request.py` | 2 | 0 | False | ✓ |
| `stock/__init__.py` | `akshare/stock/__init__.py` | 0 | 0 | False | ✓ |
| `stock/cons.py` | `akshare/stock/cons.py` | 0 | 0 | True | ✓ |
| `stock/stock_allotment_cninfo.py` | `akshare/stock/stock_allotment_cninfo.py` | 1 | 0 | False | ✓ |
| `stock/stock_ask_bid_em.py` | `akshare/stock/stock_ask_bid_em.py` | 0 | 0 | False | ✓ |
| `stock/stock_board_concept_em.py` | `akshare/stock/stock_board_concept_em.py` | 2 | 0 | False | ✓ |
| `stock/stock_board_industry_em.py` | `akshare/stock/stock_board_industry_em.py` | 2 | 0 | False | ✓ |
| `stock/stock_cg_equity_mortgage.py` | `akshare/stock/stock_cg_equity_mortgage.py` | 1 | 0 | False | ✓ |
| `stock/stock_cg_guarantee.py` | `akshare/stock/stock_cg_guarantee.py` | 1 | 0 | False | ✓ |
| `stock/stock_cg_lawsuit.py` | `akshare/stock/stock_cg_lawsuit.py` | 1 | 0 | False | ✓ |
| `stock/stock_dividend_cninfo.py` | `akshare/stock/stock_dividend_cninfo.py` | 1 | 0 | False | ✓ |
| `stock/stock_dzjy_em.py` | `akshare/stock/stock_dzjy_em.py` | 0 | 0 | False | ✓ |
| `stock/stock_fund_em.py` | `akshare/stock/stock_fund_em.py` | 3 | 0 | False | ✓ |
| `stock/stock_fund_hold.py` | `akshare/stock/stock_fund_hold.py` | 0 | 0 | False | ✓ |
| `stock/stock_gsrl_em.py` | `akshare/stock/stock_gsrl_em.py` | 0 | 0 | False | ✓ |
| `stock/stock_hk_comparison_em.py` | `akshare/stock/stock_hk_comparison_em.py` | 0 | 0 | False | ✓ |
| `stock/stock_hk_famous.py` | `akshare/stock/stock_hk_famous.py` | 1 | 0 | False | ✓ |
| `stock/stock_hk_fhpx_ths.py` | `akshare/stock/stock_hk_fhpx_ths.py` | 0 | 0 | False | ✓ |
| `stock/stock_hk_hot_rank_em.py` | `akshare/stock/stock_hk_hot_rank_em.py` | 0 | 0 | False | ✓ |
| `stock/stock_hk_sina.py` | `akshare/stock/stock_hk_sina.py` | 2 | 0 | False | ✓ |
| `stock/stock_hold_control_cninfo.py` | `akshare/stock/stock_hold_control_cninfo.py` | 1 | 0 | False | ✓ |
| `stock/stock_hold_control_em.py` | `akshare/stock/stock_hold_control_em.py` | 0 | 0 | False | ✓ |
| `stock/stock_hold_num_cninfo.py` | `akshare/stock/stock_hold_num_cninfo.py` | 1 | 0 | False | ✓ |
| `stock/stock_hot_rank_em.py` | `akshare/stock/stock_hot_rank_em.py` | 0 | 0 | False | ✓ |
| `stock/stock_hot_search_baidu.py` | `akshare/stock/stock_hot_search_baidu.py` | 0 | 0 | False | ✓ |
| `stock/stock_hot_up_em.py` | `akshare/stock/stock_hot_up_em.py` | 0 | 0 | False | ✓ |
| `stock/stock_hsgt_em.py` | `akshare/stock/stock_hsgt_em.py` | 1 | 0 | False | ✓ |
| `stock/stock_industry.py` | `akshare/stock/stock_industry.py` | 1 | 0 | False | ✓ |
| `stock/stock_industry_cninfo.py` | `akshare/stock/stock_industry_cninfo.py` | 1 | 0 | False | ✓ |
| `stock/stock_industry_pe_cninfo.py` | `akshare/stock/stock_industry_pe_cninfo.py` | 1 | 0 | False | ✓ |
| `stock/stock_industry_sw.py` | `akshare/stock/stock_industry_sw.py` | 1 | 0 | False | ✓ |
| `stock/stock_info.py` | `akshare/stock/stock_info.py` | 2 | 0 | False | ✓ |
| `stock/stock_info_em.py` | `akshare/stock/stock_info_em.py` | 0 | 0 | False | ✓ |
| `stock/stock_intraday_em.py` | `akshare/stock/stock_intraday_em.py` | 1 | 0 | False | ✓ |
| `stock/stock_intraday_sina.py` | `akshare/stock/stock_intraday_sina.py` | 1 | 0 | False | ✓ |
| `stock/stock_ipo_summary_cninfo.py` | `akshare/stock/stock_ipo_summary_cninfo.py` | 1 | 0 | False | ✓ |
| `stock/stock_new_cninfo.py` | `akshare/stock/stock_new_cninfo.py` | 1 | 0 | False | ✓ |
| `stock/stock_news_cx.py` | `akshare/stock/stock_news_cx.py` | 0 | 0 | False | ✓ |
| `stock/stock_profile_cninfo.py` | `akshare/stock/stock_profile_cninfo.py` | 1 | 0 | False | ✓ |
| `stock/stock_profile_em.py` | `akshare/stock/stock_profile_em.py` | 0 | 0 | False | ✓ |
| `stock/stock_rank_forecast.py` | `akshare/stock/stock_rank_forecast.py` | 1 | 0 | False | ✓ |
| `stock/stock_repurchase_em.py` | `akshare/stock/stock_repurchase_em.py` | 0 | 0 | False | ✓ |
| `stock/stock_share_changes_cninfo.py` | `akshare/stock/stock_share_changes_cninfo.py` | 1 | 0 | False | ✓ |
| `stock/stock_share_hold.py` | `akshare/stock/stock_share_hold.py` | 1 | 0 | False | ✓ |
| `stock/stock_stop.py` | `akshare/stock/stock_stop.py` | 0 | 0 | False | ✓ |
| `stock/stock_summary.py` | `akshare/stock/stock_summary.py` | 1 | 0 | False | ✓ |
| `stock/stock_us_famous.py` | `akshare/stock/stock_us_famous.py` | 1 | 0 | False | ✓ |
| `stock/stock_us_js.py` | `akshare/stock/stock_us_js.py` | 0 | 0 | False | ✓ |
| `stock/stock_us_pink.py` | `akshare/stock/stock_us_pink.py` | 2 | 0 | False | ✓ |
| `stock/stock_us_sina.py` | `akshare/stock/stock_us_sina.py` | 1 | 0 | False | ✓ |
| `stock/stock_weibo_nlp.py` | `akshare/stock/stock_weibo_nlp.py` | 0 | 0 | False | ✓ |
| `stock/stock_xq.py` | `akshare/stock/stock_xq.py` | 0 | 0 | False | ✓ |
| `stock/stock_zh_a_sina.py` | `akshare/stock/stock_zh_a_sina.py` | 3 | 0 | False | ✓ |
| `stock/stock_zh_a_special.py` | `akshare/stock/stock_zh_a_special.py` | 1 | 0 | False | ✓ |
| `stock/stock_zh_a_tick_163.py` | `akshare/stock/stock_zh_a_tick_163.py` | 0 | 0 | False | ✓ |
| `stock/stock_zh_a_tick_tx.py` | `akshare/stock/stock_zh_a_tick_tx.py` | 0 | 0 | False | ✓ |
| `stock/stock_zh_a_tx.py` | `akshare/stock/stock_zh_a_tx.py` | 0 | 0 | False | ✓ |
| `stock/stock_zh_ah_tx.py` | `akshare/stock/stock_zh_ah_tx.py` | 3 | 0 | False | ✓ |
| `stock/stock_zh_b_sina.py` | `akshare/stock/stock_zh_b_sina.py` | 2 | 0 | False | ✓ |
| `stock/stock_zh_comparison_em.py` | `akshare/stock/stock_zh_comparison_em.py` | 0 | 0 | False | ✓ |
| `stock/stock_zh_kcb_report.py` | `akshare/stock/stock_zh_kcb_report.py` | 0 | 0 | False | ✓ |
| `stock/stock_zh_kcb_sina.py` | `akshare/stock/stock_zh_kcb_sina.py` | 2 | 0 | False | ✓ |
| `stock_feature/stock_hist_em.py` | `akshare/stock_feature/stock_hist_em.py` | 2 | 0 | False | ✓ |
| `stock_fundamental/__init__.py` | `akshare/stock_fundamental/__init__.py` | 0 | 0 | False | ✓ |
| `stock_fundamental/stock_basic_info_xq.py` | `akshare/stock_fundamental/stock_basic_info_xq.py` | 1 | 0 | False | ✓ |
| `stock_fundamental/stock_finance_hk_em.py` | `akshare/stock_fundamental/stock_finance_hk_em.py` | 0 | 0 | False | ✓ |
| `stock_fundamental/stock_finance_sina.py` | `akshare/stock_fundamental/stock_finance_sina.py` | 1 | 0 | False | ✓ |
| `stock_fundamental/stock_finance_ths.py` | `akshare/stock_fundamental/stock_finance_ths.py` | 1 | 0 | False | ✓ |
| `stock_fundamental/stock_finance_us_em.py` | `akshare/stock_fundamental/stock_finance_us_em.py` | 1 | 0 | False | ✓ |
| `stock_fundamental/stock_gbjg_em.py` | `akshare/stock_fundamental/stock_gbjg_em.py` | 0 | 0 | False | ✓ |
| `stock_fundamental/stock_hold.py` | `akshare/stock_fundamental/stock_hold.py` | 1 | 0 | False | ✓ |
| `stock_fundamental/stock_ipo_declare.py` | `akshare/stock_fundamental/stock_ipo_declare.py` | 2 | 0 | False | ✓ |
| `stock_fundamental/stock_ipo_review.py` | `akshare/stock_fundamental/stock_ipo_review.py` | 2 | 0 | False | ✓ |
| `stock_fundamental/stock_ipo_ths.py` | `akshare/stock_fundamental/stock_ipo_ths.py` | 0 | 0 | False | ✓ |
| `stock_fundamental/stock_ipo_tutor.py` | `akshare/stock_fundamental/stock_ipo_tutor.py` | 2 | 0 | False | ✓ |
| `stock_fundamental/stock_kcb_detail_sse.py` | `akshare/stock_fundamental/stock_kcb_detail_sse.py` | 0 | 0 | False | ✓ |
| `stock_fundamental/stock_kcb_sse.py` | `akshare/stock_fundamental/stock_kcb_sse.py` | 0 | 0 | False | ✓ |
| `stock_fundamental/stock_notice.py` | `akshare/stock_fundamental/stock_notice.py` | 1 | 0 | False | ✓ |
| `stock_fundamental/stock_profit_forecast_em.py` | `akshare/stock_fundamental/stock_profit_forecast_em.py` | 1 | 0 | False | ✓ |
| `stock_fundamental/stock_profit_forecast_hk_etnet.py` | `akshare/stock_fundamental/stock_profit_forecast_hk_etnet.py` | 0 | 0 | False | ✓ |
| `stock_fundamental/stock_profit_forecast_ths.py` | `akshare/stock_fundamental/stock_profit_forecast_ths.py` | 1 | 0 | False | ✓ |
| `stock_fundamental/stock_recommend.py` | `akshare/stock_fundamental/stock_recommend.py` | 0 | 0 | False | ✓ |
| `stock_fundamental/stock_register_em.py` | `akshare/stock_fundamental/stock_register_em.py` | 2 | 0 | False | ✓ |
| `stock_fundamental/stock_restricted_em.py` | `akshare/stock_fundamental/stock_restricted_em.py` | 0 | 0 | False | ✓ |
| `stock_fundamental/stock_zygc.py` | `akshare/stock_fundamental/stock_zygc.py` | 0 | 0 | False | ✓ |
| `stock_fundamental/stock_zyjs_ths.py` | `akshare/stock_fundamental/stock_zyjs_ths.py` | 0 | 0 | False | ✓ |
| `utils/__init__.py` | `akshare/utils/__init__.py` | 0 | 0 | False | ✓ |
| `utils/cons.py` | `akshare/utils/cons.py` | 0 | 0 | False | ✓ |
| `utils/context.py` | `akshare/utils/context.py` | 0 | 0 | False | ✓ |
| `utils/demjson.py` | `akshare/utils/demjson.py` | 0 | 0 | False | ✓ |
| `utils/func.py` | `akshare/utils/func.py` | 2 | 0 | False | ✓ |
| `utils/multi_decrypt.py` | `akshare/utils/multi_decrypt.py` | 0 | 0 | False | ✓ |
| `utils/request.py` | `akshare/utils/request.py` | 0 | 0 | False | ✓ |
| `utils/token_process.py` | `akshare/utils/token_process.py` | 1 | 0 | False | ✓ |
| `utils/tqdm.py` | `akshare/utils/tqdm.py` | 0 | 0 | False | ✓ |

## 人工待办清单

（无 —— 待办清零）
