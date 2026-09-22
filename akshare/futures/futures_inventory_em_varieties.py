#!/usr/bin/env python
# -*- coding:utf-8 -*-
"""
Date: 2025/3/11 12:00
Desc: 东方财富-期货库存数据页面-期货品种清单
"""

from __future__ import annotations

from typing import Optional

import pandas as pd
import requests

FUTURES_POSITION_URL = "https://datacenter-web.eastmoney.com/api/data/v1/get"
FUTURES_POSITION_PARAMS = {
    "reportName": "RPT_FUTU_POSITIONCODE",
    "filter": '(IS_MAINCODE="1")',
    "columns": "ALL",
    "pageNumber": "1",
    "pageSize": "500",
    "sortColumns": "TRADE_CODE",
    "sortTypes": "1",
    "source": "WEB",
    "client": "WEB",
}

PLAYWRIGHT_FETCH_SCRIPT = f"""
async () => {{
    const url = new URL("{FUTURES_POSITION_URL}");
    const params = {FUTURES_POSITION_PARAMS};
    Object.entries(params).forEach(([key, value]) => url.searchParams.set(key, value));
    const response = await fetch(url.toString());
    return await response.json();
}}
"""


def _fetch_varieties_with_requests() -> Optional[pd.DataFrame]:
    r = requests.get(FUTURES_POSITION_URL, params=FUTURES_POSITION_PARAMS, timeout=20)
    if r.status_code != 200:
        return None
    data_json = r.json()
    result = data_json.get("result")
    if not result or not result.get("data"):
        return None
    return pd.DataFrame(result["data"])


def _fetch_varieties_with_playwright() -> Optional[pd.DataFrame]:
    try:
        from playwright.sync_api import sync_playwright  # type: ignore
    except ImportError:
        return None

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        context = browser.new_context()
        page = context.new_page()
        try:
            page.goto("https://data.eastmoney.com/ifdata/kcsj.html", timeout=60000)
            page.wait_for_load_state("networkidle")
            data_json = page.evaluate(PLAYWRIGHT_FETCH_SCRIPT)
            result = data_json.get("result")
            if not result or not result.get("data"):
                return None
            return pd.DataFrame(result["data"])
        finally:
            browser.close()


def futures_inventory_em_varieties(exchange: Optional[str] = None) -> pd.DataFrame:
    """
    东方财富网-数据中心-期货库存数据-品种列表
    https://data.eastmoney.com/ifdata/kcsj.html
    :param exchange: 交易所中文简称，例如 "大商所"、"上期所"、"郑商所"、"上期能源"、"中金所"、"广期所"，默认返回全部
    :type exchange: str | None
    :return: 期货品种清单
    :rtype: pandas.DataFrame
    """
    temp_df = _fetch_varieties_with_requests()
    if temp_df is None:
        temp_df = _fetch_varieties_with_playwright()

    if temp_df is None or temp_df.empty:
        raise RuntimeError("无法获取期货品种列表，请稍后重试或确认网络可访问东方财富网站。")

    temp_df = temp_df[
        [
            "TRADE_TYPE_NAME",
            "TRADE_TYPE",
            "TRADE_CODE",
            "TRADE_CODE_UPPER",
            "TRADE_MARKET_CODE",
            "EXCHANGE_TRADECODE",
            "SECURITY_CODE",
            "DERIVE_SECURITY_CODE",
            "DELIVERY_MONTH",
        ]
    ].copy()

    split_df = temp_df["TRADE_TYPE_NAME"].str.split("-", n=1, expand=True)
    if split_df.shape[1] == 1:
        split_df[1] = ""
    split_df.columns = ["交易所", "品种中文"]
    temp_df = pd.concat([temp_df, split_df], axis=1)
    temp_df.rename(
        columns={
            "TRADE_TYPE": "品种简称",
            "TRADE_CODE": "品种代码",
            "TRADE_CODE_UPPER": "品种代码大写",
            "TRADE_MARKET_CODE": "交易所编码",
            "EXCHANGE_TRADECODE": "交易所合约代码",
            "SECURITY_CODE": "主力合约代码",
            "DERIVE_SECURITY_CODE": "派生合约代码",
            "DELIVERY_MONTH": "交割月",
        },
        inplace=True,
    )

    temp_df = temp_df[
        [
            "交易所",
            "品种中文",
            "品种简称",
            "品种代码",
            "品种代码大写",
            "交易所编码",
            "交易所合约代码",
            "主力合约代码",
            "派生合约代码",
            "交割月",
        ]
    ]

    temp_df.sort_values(["交易所", "品种代码"], inplace=True, ignore_index=True)

    if exchange:
        temp_df = temp_df[temp_df["交易所"] == exchange]

    temp_df.reset_index(drop=True, inplace=True)
    return temp_df


if __name__ == "__main__":
    print(futures_inventory_em_varieties().head())
