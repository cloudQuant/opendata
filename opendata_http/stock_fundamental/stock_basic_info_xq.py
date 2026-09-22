# Ported from akshare (https://github.com/cloudQuant/akshare.git) @ c4f6a631c259783dbc2507b6b27d179b3e88079d
# Copyright (c) 2019-2026 Albert King — MIT License (see THIRD_PARTY_NOTICES.md)
# !/usr/bin/env python
# -*- coding:utf-8 -*-
"""
Date: 2025/5/16 19:00
Desc: 雪球-个股-公司概况-公司简介
https://xueqiu.com/snowman/S/SH601127/detail#/GSJJ
"""

import pandas as pd
import requests

from opendata_http.utils.cons import headers


def _get_xueqiu_session(symbol: str, token: str | None, timeout: float | None):
    session = requests.Session()
    request_headers = headers.copy()
    if token:
        request_headers["cookie"] = f"xq_a_token={token};"
    session.get(
        f"https://xueqiu.com/snowman/S/{symbol}/detail",
        headers=request_headers,
        timeout=timeout or 15,
    )
    return session, request_headers


def _get_xueqiu_company_data(
    url: str, params: dict, timeout: float | None, token: str | None = None
):
    try:
        session, request_headers = _get_xueqiu_session(params["symbol"], token, timeout)
        r = session.get(
            url, params=params, headers=request_headers, timeout=timeout or 15
        )
    except requests.RequestException as exc:
        raise RuntimeError(f"Xueqiu company endpoint request failed: {url}") from exc
    try:
        data_json = r.json()
    except ValueError as exc:
        raise RuntimeError(
            f"Xueqiu company endpoint returned non-JSON response: {url}; "
            f"preview={r.text[:120]!r}"
        ) from exc
    if r.status_code != 200:
        message = data_json.get("error_description", "")
        raise RuntimeError(
            f"Xueqiu company endpoint returned HTTP {r.status_code}: {message}"
        )
    data = data_json.get("data")
    if data is None:
        message = data_json.get("error_description") or data_json.get("error_code")
        raise RuntimeError(f"Xueqiu company endpoint returned no data: {message}")
    return data


def stock_individual_basic_info_xq(
    symbol: str = "SH601127", token: str = None, timeout: float = None
) -> pd.DataFrame:
    """
    雪球-个股-公司概况-公司简介
    https://xueqiu.com/snowman/S/SH601127/detail#/GSJJ
    :param symbol: 证券代码
    :type symbol: str
    :param token: 雪球财经的 token
    :type token: str
    :param timeout: 设置超时时间
    :type timeout: float
    :return: 公司简介
    :rtype: pandas.DataFrame
    """
    url = "https://stock.xueqiu.com/v5/stock/f10/cn/company.json"
    params = {
        "symbol": symbol,
    }
    temp_df = pd.DataFrame(_get_xueqiu_company_data(url, params, timeout, token))
    temp_df.reset_index(inplace=True)
    temp_df.columns = ["item", "value"]
    return temp_df


def stock_individual_basic_info_us_xq(
    symbol: str = "NVDA", token: str = None, timeout: float = None
) -> pd.DataFrame:
    """
    雪球-个股-公司概况-公司简介
    https://xueqiu.com/snowman/S/NVDA/detail#/GSJJ
    :param symbol: 证券代码
    :type symbol: str
    :param token: 雪球财经的 token
    :type token: str
    :param timeout: 设置超时时间
    :type timeout: float
    :return: 公司简介
    :rtype: pandas.DataFrame
    """
    url = "https://stock.xueqiu.com/v5/stock/f10/us/company.json"
    params = {
        "symbol": symbol,
    }
    temp_df = pd.DataFrame(_get_xueqiu_company_data(url, params, timeout, token))
    temp_df.reset_index(inplace=True)
    temp_df.columns = ["item", "value"]
    return temp_df


def stock_individual_basic_info_hk_xq(
    symbol: str = "02097", token: str = None, timeout: float = None
) -> pd.DataFrame:
    """
    雪球-个股-公司概况-公司简介
    https://xueqiu.com/S/00700
    :param symbol: 证券代码
    :type symbol: str
    :param token: 雪球财经的 token
    :type token: str
    :param timeout: 设置超时时间
    :type timeout: float
    :return: 公司简介
    :rtype: pandas.DataFrame
    """
    url = "https://stock.xueqiu.com/v5/stock/f10/hk/company.json"
    params = {
        "symbol": symbol,
    }
    temp_df = pd.DataFrame(_get_xueqiu_company_data(url, params, timeout, token))
    temp_df.reset_index(inplace=True)
    temp_df.columns = ["item", "value"]
    return temp_df


if __name__ == "__main__":
    stock_individual_basic_info_xq_df = stock_individual_basic_info_xq(
        symbol="SH601127"
    )
    print(stock_individual_basic_info_xq_df)

    stock_us_individual_basic_info_us_xq_df = stock_individual_basic_info_us_xq(
        symbol="NVDA"
    )
    print(stock_us_individual_basic_info_us_xq_df)

    stock_individual_basic_info_hk_xq_df = stock_individual_basic_info_hk_xq(
        symbol="02097"
    )
    print(stock_individual_basic_info_hk_xq_df)
