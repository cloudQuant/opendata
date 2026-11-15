#!/usr/bin/env python
# -*- coding:utf-8 -*-
"""
Date: 2022/4/10 18:24
Desc: 彭博亿万富豪指数
https://www.bloomberg.com/billionaires/
"""

import pandas as pd
import requests
from bs4 import BeautifulSoup

_BLOOMBERG_BILLIONAIRES_HIST_COLUMNS = [
    "rank",
    "name",
    "age",
    "country",
    "total_net_worth",
    "last_change",
    "ytd_change",
    "industry",
]


def _empty_bloomberg_billionaires_hist_df() -> pd.DataFrame:
    return pd.DataFrame(columns=_BLOOMBERG_BILLIONAIRES_HIST_COLUMNS)


def index_bloomberg_billionaires_hist(year: str = "2021") -> pd.DataFrame:
    """
    Bloomberg Billionaires Index
    https://stats.areppim.com/stats/links_billionairexlists.htm
    :param year: choice of {"2021", "2019", "2018", ...}
    :type year: str
    :return: 彭博亿万富豪指数历史数据
    :rtype: pandas.DataFrame
    """
    url = f"https://stats.areppim.com/listes/list_billionairesx{year[-2:]}xwor.htm"
    try:
        r = requests.get(url, timeout=15)
        r.raise_for_status()
    except requests.RequestException:
        return _empty_bloomberg_billionaires_hist_df()

    soup = BeautifulSoup(r.text, "lxml")
    tables = soup.find_all("table")
    if not tables:
        return _empty_bloomberg_billionaires_hist_df()

    trs = tables[0].find_all("tr")
    if not trs:
        return _empty_bloomberg_billionaires_hist_df()

    heads = trs[1] if len(trs) > 1 else trs[0]
    if "Rank" not in heads.text:
        heads = trs[0]
    dic_keys = []
    dic = {}
    for head in heads:
        head = head.text
        dic_keys.append(head)
    for dic_key in dic_keys:
        dic[dic_key] = []

    for ll in trs:
        item = ll.findAll("td")
        for i in range(len(item)):
            if i >= len(dic_keys):
                break
            v = item[i].text
            if i == 0 and not v.isdigit():
                break
            dic[dic_keys[i]].append(v)

    temp_df = pd.DataFrame(dic)
    temp_df = temp_df.rename(
        {
            "Rank": "rank",
            "Name": "name",
            "Age": "age",
            "Citizenship": "country",
            "Country": "country",
            "Net Worth(bil US$)": "total_net_worth",
            "Total net worth$Billion": "total_net_worth",
            "$ Last change": "last_change",
            "$ YTD change": "ytd_change",
            "Industry": "industry",
        },
        axis=1,
    )
    return temp_df


def index_bloomberg_billionaires() -> pd.DataFrame:
    """
    Bloomberg Billionaires Index
    https://www.bloomberg.com/billionaires/
    :return: 彭博亿万富豪指数
    :rtype: pandas.DataFrame
    """
    columns = [
        "rank",
        "name",
        "total_net_worth",
        "last_change",
        "YTD_change",
        "country",
        "industry",
    ]
    url = "https://www.bloomberg.com/billionaires"
    headers = {
        "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.9",
        "accept-encoding": "gzip, deflate, br",
        "accept-language": "zh-CN,zh;q=0.9,en;q=0.8",
        "cache-control": "no-cache",
        "pragma": "no-cache",
        "sec-fetch-dest": "document",
        "sec-fetch-mode": "navigate",
        "sec-fetch-site": "same-origin",
        "sec-fetch-user": "?1",
        "upgrade-insecure-requests": "1",
        "referer": "https://www.bloomberg.com/",
        "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/83.0.4103.116 Safari/537.36",
    }
    try:
        r = requests.get(url, headers=headers, timeout=15)
    except requests.RequestException:
        return pd.DataFrame(columns=columns)
    soup = BeautifulSoup(r.text, "lxml")
    big_content_list = list()
    table_node = soup.find(attrs={"class": "table-chart"})
    if table_node is None:
        return pd.DataFrame(columns=columns)
    soup_node = table_node.find_all(attrs={"class": "table-row"})
    for row in soup_node:
        temp_content_list = row.text.strip().replace("\n", "").split("  ")
        content_list = [item for item in temp_content_list if item != ""]
        big_content_list.append(content_list)
    if not big_content_list:
        return pd.DataFrame(columns=columns)
    temp_df = pd.DataFrame(big_content_list)
    if len(temp_df.columns) != len(columns):
        return pd.DataFrame(columns=columns)
    temp_df.columns = columns
    return temp_df


if __name__ == "__main__":
    index_bloomberg_billionaires_df = index_bloomberg_billionaires()
    print(index_bloomberg_billionaires_df)

    index_bloomberg_billionaires_hist_df = index_bloomberg_billionaires_hist(
        year="2021"
    )
    print(index_bloomberg_billionaires_hist_df)
