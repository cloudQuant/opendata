#!/usr/bin/env python
# -*- coding:utf-8 -*-
# Ported from akshare (https://github.com/cloudQuant/akshare.git) @ c4f6a631c259783dbc2507b6b27d179b3e88079d
# Copyright (c) 2019-2026 Albert King — MIT License (see THIRD_PARTY_NOTICES.md)
"""
Date: 2026/1/12 15:00
Desc: 国证指数
https://www.cnindex.com.cn/index.html
"""

import zipfile
from io import BytesIO

import pandas as pd
import requests

_INDEX_HIST_CNI_COLUMNS = [
    "日期",
    "开盘价",
    "最高价",
    "最低价",
    "收盘价",
    "涨跌幅",
    "成交量",
    "成交额",
]


def _empty_index_hist_cni_df() -> pd.DataFrame:
    return pd.DataFrame(columns=_INDEX_HIST_CNI_COLUMNS)


def index_all_cni() -> pd.DataFrame:
    """
    国证指数-最近交易日的所有指数
    https://www.cnindex.com.cn/zh_indices/sese/index.html?act_menu=1&index_type=-1
    :return: 国证指数-所有指数
    :rtype: pandas.DataFrame
    """
    url = "https://www.cnindex.com.cn/index/indexList"
    params = {
        "channelCode": "-1",
        "rows": "2000",
        "pageNum": "1",
    }
    r = requests.get(url, params=params)
    data_json = r.json()
    temp_df = pd.DataFrame(data_json["data"]["rows"])
    temp_df.columns = [
        "_",
        "_",
        "指数代码",
        "_",
        "_",
        "_",
        "_",
        "_",
        "指数简称",
        "_",
        "_",
        "_",
        "样本数",
        "收盘点位",
        "涨跌幅",
        "_",
        "PE滚动",
        "_",
        "成交量",
        "成交额",
        "总市值",
        "自由流通市值",
        "_",
        "_",
        "_",
    ]
    temp_df = temp_df[
        [
            "指数代码",
            "指数简称",
            "样本数",
            "收盘点位",
            "涨跌幅",
            "PE滚动",
            "成交量",
            "成交额",
            "总市值",
            "自由流通市值",
        ]
    ]
    temp_df["成交量"] = temp_df["成交量"] / 100000
    temp_df["成交额"] = temp_df["成交额"] / 100000000
    temp_df["总市值"] = temp_df["总市值"] / 100000000
    temp_df["自由流通市值"] = temp_df["自由流通市值"] / 100000000
    return temp_df


def index_hist_cni(
    symbol: str = "399001", start_date: str = "20230114", end_date: str = "20240114"
) -> pd.DataFrame:
    """
    指数历史行情数据
    http://www.cnindex.com.cn/module/index-detail.html?act_menu=1&indexCode=399001
    :param symbol: 指数代码
    :type symbol: str
    :param start_date: 开始时间
    :type start_date: str
    :param end_date: 结束时间
    :type end_date: str
    :return: 指数历史行情数据
    :rtype: pandas.DataFrame
    """
    start_date = "-".join([start_date[:4], start_date[4:6], start_date[6:]])
    end_date = "-".join([end_date[:4], end_date[4:6], end_date[6:]])
    url = "http://hq.cnindex.com.cn/market/market/getIndexDailyDataWithDataFormat"
    params = {
        "indexCode": symbol,
        "startDate": start_date,
        "endDate": end_date,
        "frequency": "day",
    }
    try:
        r = requests.get(url, params=params, timeout=15)
        r.raise_for_status()
        data_json = r.json()
        data_rows = data_json.get("data", {}).get("data", [])
    except (requests.RequestException, ValueError, AttributeError):
        return _empty_index_hist_cni_df()

    if not data_rows:
        return _empty_index_hist_cni_df()

    temp_df = pd.DataFrame(data_rows)
    if temp_df.empty or temp_df.shape[1] < 11:
        return _empty_index_hist_cni_df()

    temp_df = temp_df.iloc[:, :11].copy()
    temp_df.columns = [
        "日期",
        "_",
        "最高价",
        "开盘价",
        "最低价",
        "收盘价",
        "_",
        "涨跌幅",
        "成交额",
        "成交量",
        "_",
    ]
    temp_df = temp_df[
        [
            "日期",
            "开盘价",
            "最高价",
            "最低价",
            "收盘价",
            "涨跌幅",
            "成交量",
            "成交额",
        ]
    ]
    temp_df["涨跌幅"] = temp_df["涨跌幅"].str.replace("%", "")
    temp_df["涨跌幅"] = temp_df["涨跌幅"].astype("float")
    temp_df["涨跌幅"] = temp_df["涨跌幅"] / 100
    temp_df.sort_values(["日期"], inplace=True, ignore_index=True)
    temp_df["日期"] = pd.to_datetime(temp_df["日期"], errors="coerce").dt.date
    temp_df["开盘价"] = pd.to_numeric(temp_df["开盘价"], errors="coerce")
    temp_df["最高价"] = pd.to_numeric(temp_df["最高价"], errors="coerce")
    temp_df["最低价"] = pd.to_numeric(temp_df["最低价"], errors="coerce")
    temp_df["收盘价"] = pd.to_numeric(temp_df["收盘价"], errors="coerce")
    temp_df["涨跌幅"] = pd.to_numeric(temp_df["涨跌幅"], errors="coerce")
    temp_df["成交量"] = pd.to_numeric(temp_df["成交量"], errors="coerce")
    temp_df["成交额"] = pd.to_numeric(temp_df["成交额"], errors="coerce")
    return temp_df


def index_detail_cni(symbol: str = "399001") -> pd.DataFrame:
    """
    国证指数-样本详情-指定日期的样本成份
    https://www.cnindex.com.cn/module/index-detail.html?act_menu=1&indexCode=399001
    :param symbol: 指数代码
    :type symbol: str
    :return: 指定日期的样本成份
    :rtype: pandas.DataFrame
    """
    import warnings

    warnings.simplefilter(action="ignore", category=UserWarning)
    url = "https://www.cnindex.com.cn/sample-detail/download-history"
    params = {"indexcode": symbol}
    r = requests.get(url, params=params)
    temp_df = pd.read_excel(BytesIO(r.content))
    temp_df["样本代码"] = temp_df["样本代码"].astype(str).str.zfill(6)
    temp_df.columns = [
        "日期",
        "样本代码",
        "样本简称",
        "所属行业",
        "总市值",
        "权重",
    ]
    temp_df["总市值"] = pd.to_numeric(temp_df["总市值"], errors="coerce")
    temp_df["权重"] = pd.to_numeric(temp_df["权重"], errors="coerce")
    return temp_df


def index_detail_hist_cni(symbol: str = "399001") -> pd.DataFrame:
    """
    国证指数-样本详情-历史样本
    https://www.cnindex.com.cn/module/index-detail.html?act_menu=1&indexCode=399001
    :param symbol: 指数代码; "399001"
    :type symbol: str
    :return: 历史样本
    :rtype: pandas.DataFrame
    """
    url = "https://www.cnindex.com.cn/sample-detail/download-history"
    params = {"indexcode": symbol}
    r = requests.get(url, params=params)
    temp_df = pd.read_excel(BytesIO(r.content))
    temp_df["样本代码"] = temp_df["样本代码"].astype(str).str.zfill(6)
    temp_df.columns = [
        "日期",
        "样本代码",
        "样本简称",
        "所属行业",
        "总市值",
        "权重",
    ]
    temp_df["总市值"] = pd.to_numeric(temp_df["总市值"])
    temp_df["权重"] = pd.to_numeric(temp_df["权重"])
    return temp_df


def index_detail_hist_adjust_cni(symbol: str = "399005") -> pd.DataFrame:
    """
    国证指数-样本详情-历史调样
    http://www.cnindex.com.cn/module/index-detail.html?act_menu=1&indexCode=399005
    :param symbol: 指数代码
    :type symbol: str
    :return: 历史调样
    :rtype: pandas.DataFrame
    """
    url = "http://www.cnindex.com.cn/sample-detail/download-adjustment"
    params = {"indexcode": symbol}
    r = requests.get(url, params=params)
    try:
        import warnings

        with warnings.catch_warnings():
            warnings.simplefilter(action="ignore", category=UserWarning)
            temp_df = pd.read_excel(BytesIO(r.content), engine="openpyxl")
    except zipfile.BadZipFile:
        return pd.DataFrame()
    temp_df["样本代码"] = temp_df["样本代码"].astype(str).str.zfill(6)
    return temp_df


if __name__ == "__main__":
    index_all_cni_df = index_all_cni()
    print(index_all_cni_df)

    index_hist_cni_df = index_hist_cni(
        symbol="399005", start_date="20230114", end_date="20260328"
    )
    print(index_hist_cni_df)

    index_detail_cni_df = index_detail_cni(symbol="399001")
    print(index_detail_cni_df)

    index_detail_hist_cni_df = index_detail_hist_cni(symbol="399101", date="202404")
    print(index_detail_hist_cni_df)

    index_detail_hist_adjust_cni_df = index_detail_hist_adjust_cni(symbol="399005")
    print(index_detail_hist_adjust_cni_df)
