#!/usr/bin/env python
# -*- coding:utf-8 -*-
"""
Date: 2026/5/18 18:12
Desc: 百度股市通-外汇-行情榜单
https://finance.baidu.com/top/foreign-rmb
"""

import pandas as pd
import requests


_FX_QUOTE_BAIDU_COLUMNS = ["代码", "名称", "最新价", "涨跌额", "涨跌幅"]


def _parse_foreign_rank(data_json: dict) -> pd.DataFrame:
    result = data_json.get("Result") or {}
    result_list = result.get("list") if isinstance(result, dict) else None
    rows = result_list.get("body") if isinstance(result_list, dict) else None
    if rows is None and isinstance(result, list):
        rows = []
        for item in result:
            item_df = pd.DataFrame(item.get("list", [])).T
            if item_df.empty:
                continue
            values = item_df.iloc[1, :].to_dict()
            rows.append(
                {
                    "code": item.get("code"),
                    "name": item.get("name"),
                    "price": values.get("最新价"),
                    "increase": values.get("涨跌额"),
                    "ratio": values.get("涨跌幅"),
                }
            )
    rows = rows or []
    if not rows:
        return pd.DataFrame(columns=_FX_QUOTE_BAIDU_COLUMNS)

    temp_df = pd.DataFrame(rows)
    out_df = pd.DataFrame(
        {
            "代码": temp_df.get("code"),
            "名称": temp_df.get("name"),
            "最新价": temp_df.get("price"),
            "涨跌额": temp_df.get("increase"),
            "涨跌幅": temp_df.get("ratio"),
        }
    )
    out_df["最新价"] = pd.to_numeric(out_df["最新价"], errors="coerce")
    out_df["涨跌额"] = pd.to_numeric(out_df["涨跌额"], errors="coerce")
    out_df["涨跌幅"] = (
        pd.to_numeric(out_df["涨跌幅"].astype(str).str.strip("%"), errors="coerce")
        / 100
    )
    return out_df


def _foreign_rank_params(symbol_code: str, page_num: int) -> dict:
    return {
        "type": symbol_code,
        "pn": str(page_num),
        "rn": "20",
        "style": "tablelist",
        "fieldsType": "all",
        "sort_key": "",
        "sort_type": "",
        "finClientType": "pc",
    }


def _fetch_foreign_rank_with_browser(symbol_code: str) -> pd.DataFrame:
    try:
        from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise RuntimeError(
            "Baidu finance requires acs-token from browser context; install playwright "
            "or pass a valid token."
        ) from exc

    dfs = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            locale="zh-CN",
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
        )
        page = context.new_page()
        acs_token = {"value": ""}

        def on_request(request):
            if "getforeignrank" in request.url and not acs_token["value"]:
                acs_token["value"] = request.headers.get("acs-token", "")

        page.on("request", on_request)
        page.goto(
            f"https://finance.baidu.com/top/foreign-{symbol_code}",
            wait_until="domcontentloaded",
            timeout=30000,
        )
        try:
            page.wait_for_load_state("networkidle", timeout=10000)
        except PlaywrightTimeoutError:
            pass
        page.wait_for_timeout(1000)
        if not acs_token["value"]:
            browser.close()
            raise RuntimeError("Baidu finance page did not generate acs-token")

        page_num = 0
        while True:
            data_json = page.evaluate(
                """async ({params, token}) => {
                    const url = new URL("https://finance.pae.baidu.com/api/getforeignrank");
                    Object.entries(params).forEach(([key, value]) => url.searchParams.set(key, value));
                    const response = await fetch(url.toString(), {
                        headers: {
                            accept: "application/vnd.finance-web.v1+json",
                            "acs-token": token,
                        },
                    });
                    return await response.json();
                }""",
                {"params": _foreign_rank_params(symbol_code, page_num), "token": acs_token["value"]},
            )
            if data_json.get("ResultCode") != "0":
                break
            temp_df = _parse_foreign_rank(data_json)
            if temp_df.empty:
                break
            dfs.append(temp_df)
            if len(temp_df) < 20:
                break
            page_num += 20

        browser.close()
    if not dfs:
        return pd.DataFrame(columns=_FX_QUOTE_BAIDU_COLUMNS)
    return pd.concat(dfs, ignore_index=True)


def fx_quote_baidu(symbol: str = "人民币", token: str = "") -> pd.DataFrame:
    """
    百度股市通-外汇-行情榜单
    https://finance.baidu.com/top/foreign-rmb
    :param symbol: choice of {"人民币", "美元"}
    :type symbol: str
    :param token: 目标网站复制 acs-token 后传入
    :type token: str
    :return: 外汇行情数据
    :rtype: pandas.DataFrame
    """
    symbol_map = {
        "人民币": "rmb",
        "美元": "dollar",
    }
    if not token:
        return _fetch_foreign_rank_with_browser(symbol_map[symbol])

    headers = {
        "Accept": "application/vnd.finance-web.v1+json",
        "Accept-Language": "zh-CN,zh;q=0.9",
        "Origin": "https://finance.baidu.com",
        "Referer": "https://finance.baidu.com/",
        "acs-token": token,
    }
    url = "https://finance.pae.baidu.com/api/getforeignrank"
    out_df = pd.DataFrame(columns=_FX_QUOTE_BAIDU_COLUMNS)
    num = 0
    while True:
        params = _foreign_rank_params(symbol_map[symbol], num)
        r = requests.get(url, params=params, headers=headers, timeout=10)
        data_json = r.json()
        if data_json.get("ResultCode") != "0":
            print(f"[pn={num}] 接口返回异常: {data_json}")
            break
        temp_df = _parse_foreign_rank(data_json)
        if temp_df.empty:
            break
        out_df = pd.concat(objs=[out_df, temp_df], ignore_index=True)
        if len(temp_df) < 20:
            break
        num += 20
    return out_df


if __name__ == "__main__":
    fx_quote_baidu_df = fx_quote_baidu(symbol="人民币")
    print(fx_quote_baidu_df)
