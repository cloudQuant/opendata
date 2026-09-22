#!/usr/bin/env python
# -*- coding:utf-8 -*-
# Ported from akshare (https://github.com/cloudQuant/akshare.git) @ c4f6a631c259783dbc2507b6b27d179b3e88079d
# Copyright (c) 2019-2026 Albert King — MIT License (see THIRD_PARTY_NOTICES.md)
"""
Date: 2023/5/18 17:10
Desc: 中国柯桥纺织指数
http://www.kqindex.cn/flzs/jiage
"""

import pandas as pd
import requests
from tqdm import tqdm


_INDEX_KQ_FZ_COLUMNS = {
    "价格指数": ["期次", "指数", "涨跌幅"],
    "景气指数": ["期次", "总景气指数", "涨跌幅", "流通景气指数", "生产景气指数"],
    "外贸指数": ["期次", "价格指数", "价格指数-涨跌幅", "景气指数", "景气指数-涨跌幅"],
}


def index_kq_fz(symbol: str = "价格指数") -> pd.DataFrame:
    """
    中国柯桥纺织指数
    http://www.kqindex.cn/flzs/jiage
    :param symbol: choice of {'价格指数', '景气指数', '外贸指数'}
    :type symbol: str
    :return: 中国柯桥纺织指数
    :rtype: pandas.DataFrame
    """
    symbol_map = {
        "价格指数": "1_1",
        "景气指数": "1_2",
        "外贸指数": "2",
    }
    url = "http://www.kqindex.cn/flzs/table_data"
    params = {
        "category": "0",
        "start": "",
        "end": "",
        "indexType": f"{symbol_map[symbol]}",
        "pageindex": "1",
    }
    try:
        r = requests.get(url, params=params, timeout=15)
        r.raise_for_status()
        data_json = r.json()
        page_num = int(data_json["page"])
    except (requests.RequestException, ValueError, KeyError, TypeError):
        return pd.DataFrame(columns=_INDEX_KQ_FZ_COLUMNS[symbol])
    big_df = pd.DataFrame()
    for page in tqdm(range(1, page_num + 1), leave=False):
        params = {
            "category": "0",
            "start": "",
            "end": "",
            "indexType": f"{symbol_map[symbol]}",
            "pageindex": page,
        }
        try:
            r = requests.get(url, params=params, timeout=15)
            r.raise_for_status()
            data_json = r.json()
            temp_df = pd.DataFrame(data_json["result"])
        except (requests.RequestException, ValueError, KeyError, TypeError):
            continue
        big_df = pd.concat([big_df, temp_df], ignore_index=True)
    if big_df.empty:
        return pd.DataFrame(columns=_INDEX_KQ_FZ_COLUMNS[symbol])
    if symbol == "价格指数":
        big_df.columns = [
            "期次",
            "指数",
            "涨跌幅",
        ]
        big_df["期次"] = pd.to_datetime(big_df["期次"])
        big_df["指数"] = pd.to_numeric(big_df["指数"], errors="coerce")
        big_df["涨跌幅"] = pd.to_numeric(big_df["涨跌幅"], errors="coerce")
    elif symbol == "景气指数":
        big_df.columns = [
            "期次",
            "总景气指数",
            "涨跌幅",
            "流通景气指数",
            "生产景气指数",
        ]
        big_df["总景气指数"] = pd.to_numeric(big_df["总景气指数"], errors="coerce")
        big_df["涨跌幅"] = pd.to_numeric(big_df["涨跌幅"], errors="coerce")
        big_df["流通景气指数"] = pd.to_numeric(big_df["流通景气指数"], errors="coerce")
        big_df["生产景气指数"] = pd.to_numeric(big_df["生产景气指数"], errors="coerce")
    elif symbol == "外贸指数":
        big_df.columns = [
            "期次",
            "价格指数",
            "价格指数-涨跌幅",
            "景气指数",
            "景气指数-涨跌幅",
        ]
        big_df["价格指数"] = pd.to_numeric(big_df["价格指数"], errors="coerce")
        big_df["价格指数-涨跌幅"] = pd.to_numeric(
            big_df["价格指数-涨跌幅"], errors="coerce"
        )
        big_df["景气指数"] = pd.to_numeric(big_df["景气指数"], errors="coerce")
        big_df["景气指数-涨跌幅"] = pd.to_numeric(
            big_df["景气指数-涨跌幅"], errors="coerce"
        )
    big_df.sort_values(["期次"], inplace=True, ignore_index=True)
    return big_df


if __name__ == "__main__":
    index_kq_fz_df = index_kq_fz(symbol="价格指数")
    print(index_kq_fz_df)

    index_kq_fz_df = index_kq_fz(symbol="景气指数")
    print(index_kq_fz_df)

    index_kq_fz_df = index_kq_fz(symbol="外贸指数")
    print(index_kq_fz_df)
