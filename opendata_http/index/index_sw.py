#!/usr/bin/env python
# -*- coding:utf-8 -*-
# Ported from akshare (https://github.com/cloudQuant/akshare.git) @ c4f6a631c259783dbc2507b6b27d179b3e88079d
# Copyright (c) 2019-2026 Albert King — MIT License (see THIRD_PARTY_NOTICES.md)
"""
Date: 2024/1/24 15:00
Desc: 申万宏源研究-申万指数-指数发布
乐咕乐股网
https://legulegu.com/stockdata/index-composition?industryCode=851921.SI
"""

from io import StringIO

import pandas as pd
import requests
from bs4 import BeautifulSoup

from opendata_http.utils.cons import headers

_SW_INDEX_FIRST_INFO_COLUMNS = [
    "行业代码",
    "行业名称",
    "成份个数",
    "静态市盈率",
    "TTM(滚动)市盈率",
    "市净率",
    "静态股息率",
]

_SW_INDEX_HIGHER_INFO_COLUMNS = [
    "行业代码",
    "行业名称",
    "上级行业",
    "成份个数",
    "静态市盈率",
    "TTM(滚动)市盈率",
    "市净率",
    "静态股息率",
]


def _empty_sw_index_info(include_parent: bool) -> pd.DataFrame:
    return pd.DataFrame(
        columns=(
            _SW_INDEX_HIGHER_INFO_COLUMNS
            if include_parent
            else _SW_INDEX_FIRST_INFO_COLUMNS
        )
    )


def _format_sw_index_info(
    temp_df: pd.DataFrame, include_parent: bool
) -> pd.DataFrame:
    temp_df.columns = (
        _SW_INDEX_HIGHER_INFO_COLUMNS
        if include_parent
        else _SW_INDEX_FIRST_INFO_COLUMNS
    )
    temp_df["成份个数"] = pd.to_numeric(temp_df["成份个数"], errors="coerce")
    temp_df["静态市盈率"] = pd.to_numeric(temp_df["静态市盈率"], errors="coerce")
    temp_df["TTM(滚动)市盈率"] = pd.to_numeric(
        temp_df["TTM(滚动)市盈率"], errors="coerce"
    )
    temp_df["市净率"] = pd.to_numeric(temp_df["市净率"], errors="coerce")
    temp_df["静态股息率"] = pd.to_numeric(temp_df["静态股息率"], errors="coerce")
    return temp_df


def _sw_index_info(level_id: str, include_parent: bool) -> pd.DataFrame:
    url = "https://legulegu.com/stockdata/sw-industry-overview"
    try:
        r = requests.get(url, headers=headers, timeout=15)
    except Exception:
        return _empty_sw_index_info(include_parent=include_parent)
    soup = BeautifulSoup(r.text, features="lxml")
    container = soup.find(name="div", attrs={"id": level_id})
    if container is None:
        return _empty_sw_index_info(include_parent=include_parent)
    code_raw = container.find_all(
        name="div", attrs={"class": "lg-industries-item-chinese-title"}
    )
    name_raw = container.find_all(
        name="div", attrs={"class": "lg-industries-item-number"}
    )
    value_raw = container.find_all(
        name="div", attrs={"class": "lg-sw-industries-item-value"}
    )
    if not code_raw or not name_raw or not value_raw:
        return _empty_sw_index_info(include_parent=include_parent)
    rows = []
    for code_item, name_item, value_item in zip(code_raw, name_raw, value_raw):
        name_text = name_item.get_text()
        if "(" not in name_text or ")" not in name_text:
            continue
        values = [
            item.get_text().strip()
            for item in value_item.find_all("span", attrs={"class": "value"})
        ]
        values.extend([pd.NA] * (4 - len(values)))
        row = [
            code_item.get_text(),
            name_text.split("(")[0],
        ]
        if include_parent:
            parent_node = name_item.find("span")
            parent_name = ""
            if parent_node is not None:
                parent_name = parent_node.get_text().split("(")[0][1:-1]
            row.append(parent_name)
        row.extend([name_text.split("(")[1].split(")")[0], *values[:4]])
        rows.append(row)
    if not rows:
        return _empty_sw_index_info(include_parent=include_parent)
    return _format_sw_index_info(pd.DataFrame(rows), include_parent=include_parent)


def sw_index_first_info() -> pd.DataFrame:
    """
    乐咕乐股-申万一级-分类
    https://legulegu.com/stockdata/sw-industry-overview#level1
    :return: 分类
    :rtype: pandas.DataFrame
    """
    return _sw_index_info(level_id="level1Items", include_parent=False)


def sw_index_second_info() -> pd.DataFrame:
    """
    乐咕乐股-申万二级-分类
    https://legulegu.com/stockdata/sw-industry-overview#level1
    :return: 分类
    :rtype: pandas.DataFrame
    """
    return _sw_index_info(level_id="level2Items", include_parent=True)


def sw_index_third_info() -> pd.DataFrame:
    """
    乐咕乐股-申万三级-分类
    https://legulegu.com/stockdata/sw-industry-overview#level1
    :return: 分类
    :rtype: pandas.DataFrame
    """
    return _sw_index_info(level_id="level3Items", include_parent=True)


def sw_index_third_cons(symbol: str = "801120.SI") -> pd.DataFrame:
    """
    乐咕乐股-申万三级-行业成份
    https://legulegu.com/stockdata/index-composition?industryCode=801120.SI
    :param symbol: 三级行业的行业代码
    :type symbol: str
    :return: 行业成份
    :rtype: pandas.DataFrame
    """
    columns = [
        "序号",
        "股票代码",
        "股票简称",
        "纳入时间",
        "申万1级",
        "申万2级",
        "申万3级",
        "价格",
        "市盈率",
        "市盈率ttm",
        "市净率",
        "股息率",
        "市值",
        "归母净利润同比增长(09-30)",
        "归母净利润同比增长(06-30)",
        "营业收入同比增长(09-30)",
        "营业收入同比增长(06-30)",
    ]
    url = f"https://legulegu.com/stockdata/index-composition?industryCode={symbol}"
    try:
        r = requests.get(url, headers=headers, timeout=15)
        temp_df = pd.read_html(StringIO(r.text))[0]
    except Exception:
        return pd.DataFrame(columns=columns)
    if temp_df.empty:
        return pd.DataFrame(columns=columns)
    if temp_df.shape[1] > len(columns):
        temp_df = temp_df.iloc[:, : len(columns)]
    elif temp_df.shape[1] < len(columns):
        for i in range(temp_df.shape[1], len(columns)):
            temp_df[i] = pd.NA
    temp_df.columns = columns
    temp_df["价格"] = pd.to_numeric(temp_df["价格"], errors="coerce")
    temp_df["市盈率"] = pd.to_numeric(temp_df["市盈率"], errors="coerce")
    temp_df["市盈率ttm"] = pd.to_numeric(temp_df["市盈率ttm"], errors="coerce")
    temp_df["市净率"] = pd.to_numeric(temp_df["市净率"], errors="coerce")
    temp_df["股息率"] = pd.to_numeric(
        temp_df["股息率"].astype(str).str.strip("%"), errors="coerce"
    )
    temp_df["市值"] = pd.to_numeric(temp_df["市值"], errors="coerce")

    temp_df["归母净利润同比增长(09-30)"] = temp_df[
        "归母净利润同比增长(09-30)"
    ].astype(str).str.strip("%")
    temp_df["归母净利润同比增长(06-30)"] = temp_df[
        "归母净利润同比增长(06-30)"
    ].astype(str).str.strip("%")
    temp_df["营业收入同比增长(09-30)"] = temp_df[
        "营业收入同比增长(09-30)"
    ].astype(str).str.strip("%")
    temp_df["营业收入同比增长(06-30)"] = temp_df[
        "营业收入同比增长(06-30)"
    ].astype(str).str.strip("%")

    temp_df["归母净利润同比增长(09-30)"] = pd.to_numeric(
        temp_df["归母净利润同比增长(09-30)"], errors="coerce"
    )
    temp_df["归母净利润同比增长(06-30)"] = pd.to_numeric(
        temp_df["归母净利润同比增长(06-30)"], errors="coerce"
    )
    temp_df["营业收入同比增长(09-30)"] = pd.to_numeric(
        temp_df["营业收入同比增长(09-30)"], errors="coerce"
    )
    temp_df["营业收入同比增长(06-30)"] = pd.to_numeric(
        temp_df["营业收入同比增长(06-30)"], errors="coerce"
    )
    return temp_df


if __name__ == "__main__":
    sw_index_first_info_df = sw_index_first_info()
    print(sw_index_first_info_df)

    sw_index_second_info_df = sw_index_second_info()
    print(sw_index_second_info_df)

    sw_index_third_info_df = sw_index_third_info()
    print(sw_index_third_info_df)

    sw_index_third_cons_df = sw_index_third_cons(symbol="850111.SI")
    print(sw_index_third_cons_df)
