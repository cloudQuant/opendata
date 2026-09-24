#!/usr/bin/env python
# -*- coding:utf-8 -*-
# Ported from akshare (https://github.com/cloudQuant/akshare.git) @ c4f6a631c259783dbc2507b6b27d179b3e88079d
# Copyright (c) 2019-2026 Albert King — MIT License (see THIRD_PARTY_NOTICES.md)
"""
Date: 2024/1/18 16:25
Desc: 新加坡交易所-衍生品-历史数据-历史结算价格
https://www.sgx.com/zh-hans/research-education/derivatives
https://links.sgx.com/1.0.0/derivatives-daily/5888/FUTURE.zip
"""

import zipfile
from io import BytesIO
from io import StringIO

import pandas as pd
import requests

from opendata_http.utils.request import request_eastmoney


def __read_sgx_future_zip_date(content: bytes) -> str | None:
    if not content.startswith(b"PK"):
        return None
    with zipfile.ZipFile(BytesIO(content)) as file:
        with file.open(file.namelist()[0]) as my_file:
            data = my_file.read(120).decode(errors="ignore")
    for line in data.splitlines():
        if line[:8].isdigit():
            return line[:8]
    return None


def __search_sgx_future_file_num(date: str) -> int:
    target_date = pd.to_datetime(date).strftime("%Y%m%d")
    anchor_date = pd.Timestamp("2023-11-07")
    anchor_num = 6853
    query_date = pd.to_datetime(target_date)
    if query_date >= anchor_date:
        estimated_offset = len(pd.bdate_range(anchor_date, query_date)) - 1
    else:
        estimated_offset = -(len(pd.bdate_range(query_date, anchor_date)) - 1)
    estimated_num = anchor_num + estimated_offset
    for num in range(max(1, estimated_num - 90), estimated_num + 91):
        url = f"https://links.sgx.com/1.0.0/derivatives-daily/{num}/FUTURE.zip"
        try:
            r = requests.get(url, timeout=15)
        except requests.RequestException:
            continue
        file_date = __read_sgx_future_zip_date(r.content)
        if file_date == target_date:
            return num
    raise RuntimeError(f"SGX FUTURE.zip not found for date={date}")


def __fetch_ftse_index_futu(date: str = "20231108") -> int:
    """
    新加坡交易所-日历计算
    https://wap.eastmoney.com/quote/stock/100.STI.html
    :param date: 交易日
    :type date: str
    :return: 日期计算结果
    :rtype: int
    """
    url = "https://push2his.eastmoney.com/api/qt/stock/kline/get"
    params = {
        "secid": "100.STI",
        "klt": "101",
        "fqt": "0",
        "lmt": "10000",
        "end": date,
        "iscca": "1",
        "fields1": "f1,f2,f3,f4,f5,f6,f7,f8",
        "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61,f62,f63,f64",
        "ut": "f057cbcbce2a86e2866ab8877db1d059",
        "forcect": "1",
    }
    try:
        r = request_eastmoney(url, params=params, timeout=15)
        data_json = r.json()
        klines = (data_json.get("data") or {}).get("klines") or []
    except requests.RequestException:
        klines = []
    if not klines:
        return __search_sgx_future_file_num(date)
    temp_df = pd.DataFrame([item.split(",") for item in klines])
    temp_df.columns = [
        "date",
        "-",
        "open",
        "close",
        "high",
        "low",
        "volume",
        "amount",
        "_",
        "-",
        "open",
        "close",
        "high",
        "low",
    ]
    num = temp_df["date"].index[-1] + 791
    return num


def futures_settlement_price_sgx(date: str = "20231107") -> pd.DataFrame:
    """
    新加坡交易所-衍生品-历史数据-历史结算价格
    https://www.sgx.com/zh-hans/research-education/derivatives
    :param date: 交易日
    :type date: str
    :return: 所有期货品种的在指定交易日的历史结算价格
    :rtype: pandas.DataFrame
    """
    num = __fetch_ftse_index_futu(date)
    url = f"https://links.sgx.com/1.0.0/derivatives-daily/{num}/FUTURE.zip"
    r = requests.get(url)
    with zipfile.ZipFile(BytesIO(r.content)) as file:
        with file.open(file.namelist()[0]) as my_file:
            data = my_file.read().decode()
            if file.namelist()[0].endswith("txt"):
                data_df = pd.read_table(StringIO(data))
            else:
                data_df = pd.read_csv(StringIO(data))
    return data_df


if __name__ == "__main__":
    futures_settlement_price_sgx_df = futures_settlement_price_sgx(date="20240110")
    print(futures_settlement_price_sgx_df)
