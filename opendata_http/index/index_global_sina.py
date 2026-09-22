#!/usr/bin/env python
# -*- coding:utf-8 -*-
# Ported from akshare (https://github.com/cloudQuant/akshare.git) @ c4f6a631c259783dbc2507b6b27d179b3e88079d
# Copyright (c) 2019-2026 Albert King — MIT License (see THIRD_PARTY_NOTICES.md)
"""
Date: 2025/3/7 17:00
Desc: 新浪财经-行情中心-环球市场
https://finance.sina.com.cn/stock/globalindex/quotes/UKX
"""

import pandas as pd
import requests

from opendata_http.index.cons import index_global_sina_symbol_map


def index_global_name_table() -> pd.DataFrame:
    """
    新浪财经-行情中心-环球市场-名称代码映射表
    https://finance.sina.com.cn/stock/globalindex/quotes/UKX
    :return: 名称代码映射表
    :rtype: pandas.DataFrame
    """
    temp_df = pd.DataFrame.from_dict(
        index_global_sina_symbol_map, orient="index", columns=["代码"]
    )
    temp_df.index.name = "指数名称"
    temp_df.reset_index(inplace=True)
    return temp_df


def index_global_hist_sina(symbol: str | None = None) -> pd.DataFrame:
    """
    新浪财经-行情中心-环球市场-历史行情
    https://finance.sina.com.cn/stock/globalindex/quotes/UKX
    :param symbol: 指数名称；可以通过 ak.index_global_name_table() 获取; 默认为全部指数
    :type symbol: str
    :return: 环球市场历史行情
    :rtype: pandas.DataFrame
    """
    columns = ["date", "open", "high", "low", "close", "volume"]
    symbol = {"OMX": "瑞士股票指数"}.get(symbol, symbol)
    if symbol in (None, "", "全部", "all"):
        frames = []
        for index_name in index_global_sina_symbol_map:
            temp_df = index_global_hist_sina(symbol=index_name)
            if temp_df.empty:
                continue
            temp_df.insert(0, "index_name", index_name)
            frames.append(temp_df)
        if not frames:
            return pd.DataFrame(columns=["index_name", *columns])
        return pd.concat(frames, ignore_index=True)
    if symbol not in index_global_sina_symbol_map:
        return pd.DataFrame(columns=columns)
    url = "https://gi.finance.sina.com.cn/hq/daily"
    params = {
        "symbol": index_global_sina_symbol_map[symbol],
        "num": "10000",
    }
    try:
        r = requests.get(url=url, params=params, timeout=15)
        data_json = r.json()
    except (requests.RequestException, ValueError):
        return pd.DataFrame(columns=columns)
    result = data_json.get("result") if isinstance(data_json, dict) else None
    data = result.get("data") if isinstance(result, dict) else None
    if not data:
        return pd.DataFrame(columns=columns)
    temp_df = pd.DataFrame(data)
    temp_df.rename(
        columns={
            "d": "date",
            "o": "open",
            "h": "high",
            "l": "low",
            "c": "close",
            "v": "volume",
        },
        inplace=True,
    )
    if not set(columns).issubset(temp_df.columns):
        return pd.DataFrame(columns=columns)
    temp_df = temp_df[columns]
    temp_df["date"] = pd.to_datetime(temp_df["date"], errors="coerce").dt.date
    temp_df["open"] = pd.to_numeric(temp_df["open"], errors="coerce")
    temp_df["high"] = pd.to_numeric(temp_df["high"], errors="coerce")
    temp_df["low"] = pd.to_numeric(temp_df["low"], errors="coerce")
    temp_df["close"] = pd.to_numeric(temp_df["close"], errors="coerce")
    temp_df["volume"] = pd.to_numeric(temp_df["volume"], errors="coerce")
    return temp_df


if __name__ == "__main__":
    index_global_name_table_df = index_global_name_table()
    print(index_global_name_table_df)

    index_global_hist_sina_df = index_global_hist_sina(symbol="瑞士股票指数")
    print(index_global_hist_sina_df)
