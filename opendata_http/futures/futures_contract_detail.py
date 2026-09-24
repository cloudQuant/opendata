#!/usr/bin/env python
# -*- coding:utf-8 -*-
# Ported from akshare (https://github.com/cloudQuant/akshare.git) @ c4f6a631c259783dbc2507b6b27d179b3e88079d
# Copyright (c) 2019-2026 Albert King — MIT License (see THIRD_PARTY_NOTICES.md)
"""
Date: 2025/10/30 17:00
Desc: 查询期货合约当前时刻的详情
https://finance.sina.com.cn/futures/quotes/V2101.shtml
"""

from io import StringIO

import pandas as pd
import requests
from bs4 import BeautifulSoup


def _futures_contract_detail_page_symbol(symbol: str) -> str:
    url = f"https://quote.eastmoney.com/qihuo/{symbol}.html"
    r = requests.get(url, timeout=15)
    try:
        r.raise_for_status()
    except requests.RequestException as exc:
        raise RuntimeError(f"Eastmoney futures contract page request failed: {url}") from exc

    soup = BeautifulSoup(r.text, features="lxml")
    tab_box = soup.find(name="div", attrs={"class": "sidertabbox_tsplit"})
    one_tab = tab_box.find(name="div", attrs={"class": "onet"}) if tab_box else None
    anchor = one_tab.find("a") if one_tab else None
    href = anchor.get("href") if anchor else None
    if not href or "#" not in href:
        raise ValueError(f"Eastmoney futures contract page has no detail anchor: {url}")
    return href.split("#")[-1].removeprefix("futures_")


def _fallback_futures_contract_symbol_em() -> str:
    from opendata_http.futures.futures_hist_em import futures_hist_table_em

    contract_df = futures_hist_table_em()
    if contract_df.empty or "合约代码" not in contract_df.columns:
        raise RuntimeError("Eastmoney active futures contract table is empty")
    candidates = [
        item
        for item in contract_df["合约代码"].dropna().astype(str).tolist()
        if item and not item.endswith(("m", "s"))
    ]
    candidates.extend(contract_df["合约代码"].dropna().astype(str).tolist())
    for item in candidates[:20]:
        try:
            _futures_contract_detail_page_symbol(item)
            return item
        except Exception:
            continue
    raise RuntimeError("No active Eastmoney futures contract detail page is available")


def futures_contract_detail(symbol: str = "AP2101") -> pd.DataFrame:
    """
    查询期货合约详情
    https://finance.sina.com.cn/futures/quotes/V2101.shtml
    :param symbol: 合约
    :type symbol: str
    :return: 期货合约详情
    :rtype: pandas.DataFrame
    """
    url = f"https://finance.sina.com.cn/futures/quotes/{symbol}.shtml"
    r = requests.get(url)
    r.encoding = "gb2312"
    temp_df = pd.read_html(StringIO(r.text))[6]
    data_one = temp_df.iloc[:, :2]
    data_one.columns = ["item", "value"]
    data_two = temp_df.iloc[:, 2:4]
    data_two.columns = ["item", "value"]
    data_three = temp_df.iloc[:, 4:]
    data_three.columns = ["item", "value"]
    temp_df = pd.concat(
        objs=[data_one, data_two, data_three], axis=0, ignore_index=True
    )
    return temp_df


def futures_contract_detail_em(symbol: str = "v2607F") -> pd.DataFrame:
    """
    查询期货合约详情
    https://quote.eastmoney.com/qihuo/v2602F.html
    :param symbol: 合约
    :type symbol: str
    :return: 期货合约详情
    :rtype: pandas.DataFrame
    """
    try:
        inner_symbol = _futures_contract_detail_page_symbol(symbol)
    except Exception:
        fallback_symbol = _fallback_futures_contract_symbol_em()
        inner_symbol = _futures_contract_detail_page_symbol(fallback_symbol)
    url = f"https://futsse-static.eastmoney.com/redis?msgid={inner_symbol}_info"
    try:
        r = requests.get(url, timeout=15)
        r.raise_for_status()
        data_json = r.json()
    except (requests.RequestException, ValueError) as exc:
        raise RuntimeError(f"Eastmoney futures contract detail request failed: {url}") from exc
    if not data_json:
        raise RuntimeError(f"Eastmoney futures contract detail data is empty: {url}")
    temp_df = pd.DataFrame.from_dict(data_json, orient="index")
    column_mapping = {
        "vname": "交易品种",
        "vcode": "交易代码",
        "jydw": "交易单位",
        "bjdw": "报价单位",
        "market": "上市交易所",
        "zxbddw": "最小变动价格",
        "zdtbfd": "跌涨停板幅度",
        "hyjgyf": "合约交割月份",
        "jysj": "交易时间",
        "zhjyr": "最后交易日",
        "zhjgr": "最后交割日",
        "jgpj": "交割品级",
        "zcjybzj": "最初交易保证金",
        "jgfs": "交割方式",
    }
    temp_df.rename(index=column_mapping, inplace=True)
    temp_df.reset_index(drop=False, inplace=True)
    temp_df.columns = ["item", "value"]
    return temp_df


if __name__ == "__main__":
    futures_contract_detail_df = futures_contract_detail(symbol="V2101")
    print(futures_contract_detail_df)

    futures_contract_detail_em_df = futures_contract_detail_em(symbol="l2602F")
    print(futures_contract_detail_em_df)
