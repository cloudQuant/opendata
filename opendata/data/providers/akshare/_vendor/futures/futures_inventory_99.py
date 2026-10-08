#!/usr/bin/env python
# -*- coding:utf-8 -*-
# Ported from akshare (https://github.com/cloudQuant/akshare.git) @ c4f6a631c259783dbc2507b6b27d179b3e88079d
# Copyright (c) 2019-2026 Albert King — MIT License (see THIRD_PARTY_NOTICES.md)
"""
Date: 2025/11/25 18:00
Desc: 99 期货网-大宗商品库存数据
https://www.99qh.com/
"""

import json
from functools import lru_cache

import pandas as pd
import requests
from bs4 import BeautifulSoup


def __fetch_99_stock_page(product_id=None) -> dict:
    """
    99 期货网-库存页面内置数据
    https://www.99qh.com/data/stockIn?productId=12
    :param product_id: 品种编号
    :return: 页面内置数据
    :rtype: dict
    """
    url = "https://www.99qh.com/data/stockIn"
    params = {"productId": product_id} if product_id is not None else None
    headers = {
        "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                      "AppleWebKit/537.36 (KHTML, like Gecko) "
                      "Chrome/120.0.0.0 Safari/537.36",
        "referer": "https://www.99qh.com/data/stockIn",
    }
    r = requests.get(url, params=params, headers=headers, verify=False, timeout=15)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, features="lxml")
    raw_data = soup.find(attrs={"id": "__NEXT_DATA__"})
    if raw_data is None or not raw_data.text:
        raise ValueError("未找到 99 期货库存页面内置数据")
    data_json = json.loads(raw_data.text)
    return data_json["props"]["pageProps"]["data"]


@lru_cache(maxsize=32)
def __get_99_symbol_map() -> pd.DataFrame:
    """
    99 期货网-品种代码对照表
    https://www.99qh.com/data/stockIn?productId=12
    :return: 品种代码对照表
    :rtype: pandas.DataFrame
    """
    import warnings
    warnings.filterwarnings("ignore")
    data_json = __fetch_99_stock_page()
    df_list = []
    for i, item in enumerate(data_json["varietyListData"]):
        temp_df = pd.DataFrame(
            data_json["varietyListData"][i]["productList"]
        )
        df_list.append(temp_df)

    if not df_list:
        return pd.DataFrame()
    big_df = pd.concat(df_list, ignore_index=True)
    return big_df


def futures_inventory_99(symbol: str = "豆一") -> pd.DataFrame:
    """
    99 期货网-大宗商品库存数据
    https://www.99qh.com/data/stockIn?productId=12
    :param symbol: 交易所对应的具体品种; 如：大连商品交易所的 豆一
    :type symbol: str
    :return: 大宗商品库存数据
    :rtype: pandas.DataFrame
    """
    import warnings
    warnings.filterwarnings("ignore")
    temp_df = __get_99_symbol_map()
    symbol_name_map = dict(zip(temp_df["name"], temp_df["productId"]))
    symbol_code_map = {
        str(code).lower(): product_id
        for code, product_id in zip(temp_df["code"], temp_df["productId"])
    }
    symbol = symbol.strip()
    if symbol in symbol_name_map:  # 如果输入的是中文名称
        product_id = symbol_name_map[symbol]
    elif symbol.lower() in symbol_code_map:  # 如果输入的是代码
        product_id = symbol_code_map[symbol.lower()]
    else:
        raise ValueError(f"未找到品种 {symbol} 对应的编号")

    data_json = __fetch_99_stock_page(product_id=product_id)
    stock_list = data_json.get("positionTrendChartListData", {}).get("list", [])
    if stock_list:
        temp_df = pd.DataFrame(stock_list, columns=["日期", "收盘价", "库存"])
    else:
        stock_list = data_json.get("positionTrendTableListData", {}).get("list", [])
        temp_df = pd.DataFrame(stock_list)
        if temp_df.empty:
            return pd.DataFrame(columns=["日期", "收盘价", "库存"])
        temp_df.rename(
            columns={"date": "日期", "close": "收盘价", "stock": "库存"},
            inplace=True,
        )
        temp_df = temp_df[["日期", "收盘价", "库存"]]
    temp_df.sort_values(by=["日期"], ignore_index=True, inplace=True)
    temp_df["日期"] = pd.to_datetime(temp_df["日期"], errors="coerce").dt.date
    temp_df["收盘价"] = pd.to_numeric(temp_df["收盘价"], errors="coerce")
    temp_df["库存"] = pd.to_numeric(temp_df["库存"], errors="coerce")
    return temp_df


if __name__ == "__main__":
    futures_inventory_99_df = futures_inventory_99(symbol="豆一")
    print(futures_inventory_99_df)
