#!/usr/bin/env python
# -*- coding:utf-8 -*-
"""
Date: 2022/6/28 14:57
Desc: 中国外汇交易中心暨全国银行间同业拆借中心-市场数据-市场行情-外汇市场行情
人民币外汇即期报价: fx_spot_quote
人民币外汇远掉报价: fx_swap_quote
外币对即期报价: fx_pair_quote
"""

import time
from typing import Any

import pandas as pd
import requests

from opendata.data.providers.akshare._vendor.fx.cons import (
    SHORT_HEADERS,
    FX_SPOT_URL,
    FX_SWAP_URL,
    FX_PAIR_URL,
)


def fx_spot_quote(include_timestamp: bool = False) -> pd.DataFrame:
    """
    中国外汇交易中心暨全国银行间同业拆借中心-市场数据-市场行情-外汇市场行情-人民币外汇即期报价
    http://www.chinamoney.com.cn/chinese/mkdatapfx/
    :param include_timestamp: 保留源中间价、上海时间的源发布时间、来源和报价原始值；默认保持三列兼容
    :type include_timestamp: bool
    :return: 人民币外汇即期报价
    :rtype: pandas.DataFrame
    """
    payload = {"t": str(int(round(time.time() * 1000)))}
    res = requests.post(FX_SPOT_URL, data=payload, headers=SHORT_HEADERS, timeout=20)
    res.raise_for_status()
    # Parse JSON numbers as text so optional raw bid/ask fields retain their
    # original decimal lexemes before pandas converts the public numeric columns.
    body = res.json(
        parse_float=str,
        parse_int=str,
        parse_constant=_reject_nonfinite_json_constant,
        object_pairs_hook=_reject_duplicate_object_keys,
    )
    if not isinstance(body, dict) or not isinstance(body.get("records"), list):
        raise ValueError("CFETS spot response omitted its records list")
    temp_df = pd.DataFrame(body["records"])
    if not temp_df.empty and not {"ccyPair", "bidPrc", "askPrc"}.issubset(temp_df):
        raise ValueError("CFETS spot response omitted required quote fields")
    temp_df = temp_df.reindex(columns=["ccyPair", "bidPrc", "askPrc", "midprice"])
    temp_df.columns = ["货币对", "买报价", "卖报价", "中间价"]
    if include_timestamp:
        temp_df["买报价原始值"] = temp_df["买报价"].map(_source_quote_value)
        temp_df["卖报价原始值"] = temp_df["卖报价"].map(_source_quote_value)
        temp_df["中间价原始值"] = temp_df["中间价"].map(_source_quote_value)
    temp_df["买报价"] = pd.to_numeric(temp_df["买报价"], errors="coerce")
    temp_df["卖报价"] = pd.to_numeric(temp_df["卖报价"], errors="coerce")
    if include_timestamp:
        metadata = body.get("data") or {}
        stamp = pd.to_datetime(metadata.get("showDateCN"), errors="coerce")
        if not temp_df.empty and pd.isna(stamp):
            raise ValueError(
                "CFETS spot response omitted a valid source publication time"
            )
        temp_df["中间价"] = pd.to_numeric(temp_df["中间价"], errors="coerce")
        temp_df["报价时间"] = stamp
        temp_df["报价来源"] = "CFETS"
    else:
        temp_df = temp_df[["货币对", "买报价", "卖报价"]]
    return temp_df


def _source_quote_value(value: Any) -> str | None:
    """Retain the source's bid/ask representation before numeric coercion."""
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    return value if isinstance(value, str) else str(value)


def _reject_duplicate_object_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"CFETS spot response contains duplicate JSON key {key!r}")
        result[key] = value
    return result


def _reject_nonfinite_json_constant(value: str) -> None:
    raise ValueError(f"CFETS spot response contains non-finite JSON token {value}")


def fx_swap_quote() -> pd.DataFrame:
    """
    中国外汇交易中心暨全国银行间同业拆借中心-市场数据-市场行情-债券市场行情-人民币外汇远掉报价
    https://www.chinamoney.com.cn/chinese/index.html
    :return: 人民币外汇远掉报价
    :rtype: pandas.DataFrame
    """
    payload = {"t": str(int(round(time.time() * 1000)))}
    res = requests.post(FX_SWAP_URL, data=payload, headers=SHORT_HEADERS)
    temp_df = pd.DataFrame(res.json()["records"])
    temp_df = temp_df[
        [
            "ccyPair",
            "label_1W",
            "label_1M",
            "label_3M",
            "label_6M",
            "label_9M",
            "label_1Y",
        ]
    ]
    temp_df.columns = [
        "货币对",
        "1周",
        "1月",
        "3月",
        "6月",
        "9月",
        "1年",
    ]
    return temp_df


def fx_pair_quote() -> pd.DataFrame:
    """
    中国外汇交易中心暨全国银行间同业拆借中心-市场数据-市场行情-债券市场行情-外币对即期报价
    http://www.chinamoney.com.cn/chinese/mkdatapfx/
    :return: 外币对即期报价
    :rtype: pandas.DataFrame
    """
    payload = {"t": str(int(round(time.time() * 1000)))}
    res = requests.post(FX_PAIR_URL, data=payload, headers=SHORT_HEADERS)
    temp_df = pd.DataFrame(res.json()["records"])
    temp_df = temp_df[["ccyPair", "bidPrc", "askPrc", "midprice", "time"]]
    temp_df.columns = [
        "货币对",
        "买报价",
        "卖报价",
        "-",
        "-",
    ]
    temp_df = temp_df[["货币对", "买报价", "卖报价"]]
    temp_df["买报价"] = pd.to_numeric(temp_df["买报价"], errors="coerce")
    temp_df["卖报价"] = pd.to_numeric(temp_df["卖报价"], errors="coerce")
    return temp_df


if __name__ == "__main__":
    fx_spot_quote_df = fx_spot_quote()
    print(fx_spot_quote_df)

    fx_swap_quote_df = fx_swap_quote()
    print(fx_swap_quote_df)

    fx_pair_quote_df = fx_pair_quote()
    print(fx_pair_quote_df)
