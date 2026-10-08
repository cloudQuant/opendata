#!/usr/bin/env python
# -*- coding:utf-8 -*-
# Ported from akshare (https://github.com/cloudQuant/akshare.git) @ c4f6a631c259783dbc2507b6b27d179b3e88079d
# Copyright (c) 2019-2026 Albert King — MIT License (see THIRD_PARTY_NOTICES.md)
"""
Date: 2025/6/16 18:19
Desc: 百度股市通-热搜股票
https://gushitong.baidu.com/hotlist?mainTab=hotSearch&market=all
"""

from datetime import datetime

import pandas as pd
import requests


def stock_hot_search_baidu(
    symbol: str = "A股", date: str = "20250616", time: str = "今日"
):
    """
    百度股市通-热搜股票
    https://gushitong.baidu.com/hotlist?mainTab=hotSearch&market=all
    :param symbol: choice of {"全部", "A股", "港股", "美股"}
    :type symbol: str
    :param date: 日期
    :type date: str
    :param time: time="今日"；choice of {"今日", "1小时"}
    :type time: str
    :return: 热搜股票
    :rtype: pandas.DataFrame
    """
    hour_str = datetime.now().hour
    symbol_map = {
        "全部": "all",
        "全市场": "all",
        "A股": "ab",
        "港股": "hk",
        "美股": "us",
    }
    columns = ["名称/代码", "涨跌幅", "综合热度"]
    url = "https://finance.pae.baidu.com/selfselect/listsugrecomm"
    params = {
        "bizType": "wisexmlnew",
        "dsp": "iphone",
        "product": "search",
        "style": "tablelist",
        "market": symbol_map[symbol],
        "type": time,
        "day": date,
        "hour": hour_str,
        "pn": "0",
        "rn": "12",
        "finClientType": "pc",
    }
    r = requests.get(url, params=params)
    data_json = r.json()
    result_json = data_json.get("Result", {})
    if not isinstance(result_json, dict):
        return pd.DataFrame(columns=columns)
    list_json = result_json.get("list", {})
    if not isinstance(list_json, dict):
        return pd.DataFrame(columns=columns)
    body_json = list_json.get("body", [])
    if not body_json:
        return pd.DataFrame(columns=columns)
    temp_df = pd.DataFrame(body_json)
    temp_df.rename(
        columns={
            "name": "名称/代码",
            "pxChangeRate": "涨跌幅",
            "heat": "综合热度",
        },
        inplace=True,
    )
    temp_df = temp_df[
        columns
    ]
    temp_df["综合热度"] = pd.to_numeric(temp_df["综合热度"], errors="coerce")
    return temp_df


if __name__ == "__main__":
    stock_hot_search_baidu_df = stock_hot_search_baidu(
        symbol="A股", date="20250616", time="今日"
    )
    print(stock_hot_search_baidu_df)
