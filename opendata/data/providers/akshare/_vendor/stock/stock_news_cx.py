#!/usr/bin/env python
# -*- coding:utf-8 -*-
# Ported from akshare (https://github.com/cloudQuant/akshare.git) @ c4f6a631c259783dbc2507b6b27d179b3e88079d
# Copyright (c) 2019-2026 Albert King — MIT License (see THIRD_PARTY_NOTICES.md)
"""
Date: 2025/12/25 17:00
Desc: 财新网-财新数据通
https://cxdata.caixin.com/pc/
"""

import pandas as pd
import requests


_STOCK_NEWS_MAIN_CX_COLUMNS = ["tag", "summary", "url"]


def stock_news_main_cx() -> pd.DataFrame:
    """
    财新网-财新数据通
    https://cxdata.caixin.com/pc/
    :return: 特定时间表示的字典
    :rtype: pandas.DataFrame
    """
    url = "https://cxdata.caixin.com/api/dataplus/sjtPc/news"
    params = {
        "pageNum": "1",
        "pageSize": "100",
        "showLabels": "true",
    }
    headers = {
        "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/143.0.0.0 Safari/537.36",
        "referer": "https://cxdata.caixin.com/index/newsTab?tab=latest",
    }
    try:
        r = requests.get(url, params=params, headers=headers, timeout=15)
        r.raise_for_status()
        data_json = r.json()
        temp_df = pd.DataFrame(data_json["data"]["data"])
        temp_df = temp_df[_STOCK_NEWS_MAIN_CX_COLUMNS]
    except (requests.RequestException, ValueError, KeyError, TypeError):
        return pd.DataFrame(columns=_STOCK_NEWS_MAIN_CX_COLUMNS)
    temp_df.columns = _STOCK_NEWS_MAIN_CX_COLUMNS
    temp_df.dropna(inplace=True)
    return temp_df


if __name__ == "__main__":
    stock_news_main_cx_df = stock_news_main_cx()
    print(stock_news_main_cx_df)
