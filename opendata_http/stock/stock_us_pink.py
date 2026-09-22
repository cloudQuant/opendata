#!/usr/bin/env python
# -*- coding:utf-8 -*-
# Ported from akshare (https://github.com/cloudQuant/akshare.git) @ c4f6a631c259783dbc2507b6b27d179b3e88079d
# Copyright (c) 2019-2026 Albert King — MIT License (see THIRD_PARTY_NOTICES.md)
"""
Date: 2025/6/17 14:00
Desc: 东方财富网-行情中心-美股市场-粉单市场
https://quote.eastmoney.com/center/gridlist.html#us_pinksheet
"""

import pandas as pd
import requests

from opendata_http.utils.request import request_eastmoney
from opendata_http.utils.tqdm import get_tqdm


def stock_us_pink_spot_em() -> pd.DataFrame:
    """
    东方财富网-行情中心-美股市场-粉单市场
    https://quote.eastmoney.com/center/gridlist.html#us_pinksheet
    :return: 粉单市场实时行情
    :rtype: pandas.DataFrame
    """
    url = "https://23.push2.eastmoney.com/api/qt/clist/get"
    params = {
        "np": "1",
        "fltt": "1",
        "invt": "1",
        "fs": "m:153",
        "fields": "f1,f2,f3,f4,f5,f6,f7,f8,f9,f10,f12,f13,f14,f15,f16,f17,f18,f20,f21,f23,f24,f25,"
        "f26,f22,f33,f11,f62,f128,f136,f115,f152",
        "fid": "f3",
        "pn": "1",
        "pz": "100",
        "po": "1",
        "dect": "1",
        "ut": "bd1d9ddb04089700cf9c27f6f7426281",
    }
    try:
        r = request_eastmoney(url, params=params, timeout=15)
    except requests.RequestException as exc:
        raise RuntimeError(f"Eastmoney US pink endpoint request failed: {url}") from exc
    if r.status_code != 200:
        raise RuntimeError(
            f"Eastmoney US pink endpoint returned HTTP {r.status_code}: {url}"
        )
    try:
        data_json = r.json()
    except ValueError as exc:
        raise RuntimeError(
            f"Eastmoney US pink endpoint returned non-JSON response: {url}; "
            f"preview={r.text[:120]!r}"
        ) from exc
    import math

    data = data_json.get("data") or {}
    if not data.get("diff"):
        return pd.DataFrame()
    total_page = math.ceil(data["total"] / 100)
    tqdm = get_tqdm()
    big_df = pd.DataFrame()
    for page in tqdm(range(1, total_page + 1), leave=False):
        params.update({"pn": page})
        try:
            r = request_eastmoney(url, params=params, timeout=15)
        except requests.RequestException as exc:
            raise RuntimeError(f"Eastmoney US pink endpoint request failed: {url}") from exc
        if r.status_code != 200:
            raise RuntimeError(
                f"Eastmoney US pink endpoint returned HTTP {r.status_code}: {url}"
            )
        try:
            data_json = r.json()
        except ValueError as exc:
            raise RuntimeError(
                f"Eastmoney US pink endpoint returned non-JSON response: {url}; "
                f"preview={r.text[:120]!r}"
            ) from exc
        temp_df = pd.DataFrame((data_json.get("data") or {}).get("diff") or [])
        big_df = pd.concat(objs=[big_df, temp_df], ignore_index=True)
    big_df.columns = [
        "_",
        "最新价",
        "涨跌幅",
        "涨跌额",
        "_",
        "_",
        "_",
        "_",
        "_",
        "_",
        "_",
        "简称",
        "编码",
        "名称",
        "最高价",
        "最低价",
        "开盘价",
        "昨收价",
        "总市值",
        "_",
        "_",
        "_",
        "_",
        "_",
        "_",
        "_",
        "_",
        "市盈率",
        "_",
        "_",
        "_",
        "_",
        "_",
    ]
    big_df.reset_index(inplace=True)
    big_df["index"] = range(1, len(big_df) + 1)
    big_df.rename(columns={"index": "序号"}, inplace=True)
    big_df["代码"] = big_df["编码"].astype(str) + "." + big_df["简称"]
    big_df = big_df[
        [
            "序号",
            "名称",
            "最新价",
            "涨跌额",
            "涨跌幅",
            "开盘价",
            "最高价",
            "最低价",
            "昨收价",
            "总市值",
            "市盈率",
            "代码",
        ]
    ]
    big_df["最新价"] = pd.to_numeric(big_df["最新价"], errors="coerce")
    big_df["涨跌额"] = pd.to_numeric(big_df["涨跌额"], errors="coerce")
    big_df["涨跌幅"] = pd.to_numeric(big_df["涨跌幅"], errors="coerce")
    big_df["开盘价"] = pd.to_numeric(big_df["开盘价"], errors="coerce")
    big_df["最高价"] = pd.to_numeric(big_df["最高价"], errors="coerce")
    big_df["最低价"] = pd.to_numeric(big_df["最低价"], errors="coerce")
    big_df["昨收价"] = pd.to_numeric(big_df["昨收价"], errors="coerce")
    big_df["总市值"] = pd.to_numeric(big_df["总市值"], errors="coerce")
    big_df["市盈率"] = pd.to_numeric(big_df["市盈率"], errors="coerce")
    return big_df


if __name__ == "__main__":
    stock_us_pink_spot_em_df = stock_us_pink_spot_em()
    print(stock_us_pink_spot_em_df)
