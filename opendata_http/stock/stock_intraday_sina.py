#!/usr/bin/env python
# -*- coding:utf-8 -*-
# Ported from akshare (https://github.com/cloudQuant/akshare.git) @ c4f6a631c259783dbc2507b6b27d179b3e88079d
# Copyright (c) 2019-2026 Albert King — MIT License (see THIRD_PARTY_NOTICES.md)
"""
Date: 2025/9/28 13:30
Desc: 新浪财经-日内分时数据
https://quote.eastmoney.com/f1.html?newcode=0.000001
"""

import math
from datetime import datetime, timedelta

import pandas as pd
import requests

from opendata_http.utils.tqdm import get_tqdm


def stock_intraday_sina(
    symbol: str = "sz000001", date: str | None = None
) -> pd.DataFrame:
    """
    新浪财经-日内分时数据
    https://vip.stock.finance.sina.com.cn/quotes_service/view/cn_bill.php?symbol=sz000001
    :param symbol: 股票代码
    :type symbol: str
    :param date: 交易日; 不传时自动向前查找最近可用交易日
    :type date: str
    :return: 分时数据
    :rtype: pandas.DataFrame
    """
    if date:
        date_candidates = [date]
    else:
        today = datetime.now().date()
        date_candidates = [
            (today - timedelta(days=offset)).strftime("%Y%m%d")
            for offset in range(10)
        ]
    url = "https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/CN_Bill.GetBillListCount"
    headers = {
        "Referer": f"https://vip.stock.finance.sina.com.cn/quotes_service/view/cn_bill.php?symbol={symbol}",
        "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/107.0.0.0 Safari/537.36",
    }
    for candidate_date in date_candidates:
        params = {
            "symbol": f"{symbol}",
            "num": "60",
            "page": "1",
            "sort": "ticktime",
            "asc": "0",
            "volume": "0",
            "amount": "0",
            "type": "0",
            "day": "-".join(
                [candidate_date[:4], candidate_date[4:6], candidate_date[6:]]
            ),
        }
        r = requests.get(url=url, params=params, headers=headers, timeout=15)
        r.raise_for_status()
        data_json = r.json()
        total_page = math.ceil(int(data_json or 0) / 60)
        if total_page <= 0:
            if date:
                return pd.DataFrame()
            continue
        detail_url = "https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/CN_Bill.GetBillList"
        big_df = pd.DataFrame()
        tqdm = get_tqdm()
        for page in tqdm(range(1, total_page + 1), leave=False):
            params.update({"page": page})
            r = requests.get(url=detail_url, params=params, headers=headers, timeout=15)
            r.raise_for_status()
            data_json = r.json()
            temp_df = pd.DataFrame(data_json)
            if temp_df.empty:
                continue
            big_df = pd.concat(objs=[big_df, temp_df], ignore_index=True)
        if big_df.empty or "ticktime" not in big_df.columns:
            if date:
                return pd.DataFrame()
            continue
        big_df.sort_values(by=["ticktime"], inplace=True, ignore_index=True)
        big_df["price"] = pd.to_numeric(big_df["price"], errors="coerce")
        big_df["volume"] = pd.to_numeric(big_df["volume"], errors="coerce")
        big_df["prev_price"] = pd.to_numeric(big_df["prev_price"], errors="coerce")
        return big_df
    return pd.DataFrame()


if __name__ == "__main__":
    stock_intraday_sina_df = stock_intraday_sina(symbol="sz000001", date="20250926")
    print(stock_intraday_sina_df)
