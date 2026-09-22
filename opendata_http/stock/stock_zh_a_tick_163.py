#!/usr/bin/env python
# -*- coding:utf-8 -*-
# Ported from akshare (https://github.com/cloudQuant/akshare.git) @ c4f6a631c259783dbc2507b6b27d179b3e88079d
# Copyright (c) 2019-2026 Albert King — MIT License (see THIRD_PARTY_NOTICES.md)
"""
Date: 2026/06/23
Desc: 网易财经-股票-历史成交明细
"""

from io import BytesIO, StringIO

import pandas as pd
import requests


_STOCK_ZH_A_TICK_163_COLUMNS = [
    "成交时间",
    "成交价格",
    "价格变动",
    "成交量",
    "成交额",
    "性质",
]


def _empty_stock_zh_a_tick_163_df() -> pd.DataFrame:
    return pd.DataFrame(columns=_STOCK_ZH_A_TICK_163_COLUMNS)


def _format_163_code(code: str) -> str:
    """
    网易财经历史分笔下载地址使用 0/1 + 六位股票代码:
    上海市场为 0, 深圳市场为 1。
    """
    code_text = str(code).strip().lower()
    if code_text.startswith(("sh", "sz")):
        market = code_text[:2]
        symbol = code_text[2:]
    else:
        symbol = code_text[-6:]
        market = "sh" if symbol.startswith(("6", "9")) else "sz"

    market_prefix = "0" if market == "sh" else "1"
    return f"{market_prefix}{symbol}"


def _read_163_payload(content: bytes) -> pd.DataFrame:
    try:
        return pd.read_excel(BytesIO(content))
    except Exception:
        pass

    for encoding in ("gbk", "gb18030", "utf-8"):
        try:
            text_data = content.decode(encoding)
        except UnicodeDecodeError:
            continue

        try:
            table_list = pd.read_html(StringIO(text_data))
            if table_list:
                return table_list[0]
        except Exception:
            pass

        try:
            return pd.read_csv(StringIO(text_data), sep="\t")
        except Exception:
            continue

    return pd.DataFrame()


def stock_zh_a_tick_163(
    code: str = "sh600848", trade_date: str = "20210127"
) -> pd.DataFrame:
    """
    网易财经-历史分笔数据
    http://quotes.money.163.com/trade/cjmx_600000.html

    :param code: 股票代码, 如 sh600848
    :type code: str
    :param trade_date: 交易日, 如 20210127
    :type trade_date: str
    :return: 历史分笔数据
    :rtype: pandas.DataFrame
    """
    trade_date = str(trade_date).replace("-", "")
    if len(trade_date) != 8 or not trade_date.isdigit():
        return _empty_stock_zh_a_tick_163_df()

    file_code = _format_163_code(code)
    url = (
        f"http://quotes.money.163.com/cjmx/"
        f"{trade_date[:4]}/{trade_date}/{file_code}.xls"
    )
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/125.0.0.0 Safari/537.36"
        ),
        "Referer": f"http://quotes.money.163.com/trade/cjmx_{file_code[1:]}.html",
    }

    try:
        r = requests.get(url, headers=headers, timeout=15)
    except requests.RequestException:
        return _empty_stock_zh_a_tick_163_df()

    content_type = r.headers.get("Content-Type", "").lower()
    first_bytes = r.content[:200].lower()
    if (
        r.status_code != 200
        or not r.content
        or b"<html" in first_bytes
        or "text/html" in content_type
    ):
        return _empty_stock_zh_a_tick_163_df()

    temp_df = _read_163_payload(r.content)
    if temp_df.empty:
        return _empty_stock_zh_a_tick_163_df()

    temp_df.rename(
        columns={
            "时间": "成交时间",
            "成交价": "成交价格",
            "价格": "成交价格",
            "成交量(手)": "成交量",
            "成交额(元)": "成交额",
            "成交金额": "成交额",
        },
        inplace=True,
    )

    if not set(_STOCK_ZH_A_TICK_163_COLUMNS).issubset(set(temp_df.columns)):
        if temp_df.shape[1] < len(_STOCK_ZH_A_TICK_163_COLUMNS):
            return _empty_stock_zh_a_tick_163_df()
        temp_df = temp_df.iloc[:, : len(_STOCK_ZH_A_TICK_163_COLUMNS)].copy()
        temp_df.columns = _STOCK_ZH_A_TICK_163_COLUMNS

    temp_df = temp_df[_STOCK_ZH_A_TICK_163_COLUMNS].copy()
    temp_df.dropna(subset=["成交时间"], inplace=True)
    if temp_df.empty:
        return _empty_stock_zh_a_tick_163_df()

    for column in ["成交价格", "价格变动", "成交量", "成交额"]:
        temp_df[column] = pd.to_numeric(temp_df[column], errors="coerce")

    temp_df.reset_index(drop=True, inplace=True)
    return temp_df


if __name__ == "__main__":
    stock_zh_a_tick_163_df = stock_zh_a_tick_163(
        code="sh600848", trade_date="20210127"
    )
    print(stock_zh_a_tick_163_df)
