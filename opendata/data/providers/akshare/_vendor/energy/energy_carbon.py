#!/usr/bin/env python
# -*- coding:utf-8 -*-
"""
Date: 2024/6/25 15:00
Desc: 碳排放交易
北京市碳排放权电子交易平台-北京市碳排放权公开交易行情
https://www.bjets.com.cn/article/jyxx/

深圳碳排放交易所-国内碳情
http://www.cerx.cn/dailynewsCN/index.htm

深圳碳排放交易所-国际碳情
http://www.cerx.cn/dailynewsOuter/index.htm

湖北碳排放权交易中心-现货交易数据-配额-每日概况
http://www.cerx.cn/dailynewsOuter/index.htm

广州碳排放权交易中心-行情信息
http://www.cnemission.com/article/hqxx/
"""

from io import StringIO

import pandas as pd
import requests
from bs4 import BeautifulSoup
from tqdm import tqdm

from opendata.data.providers.akshare._vendor.utils import demjson
from opendata.data.providers.akshare._vendor.utils.cons import headers


_ENERGY_CARBON_EU_COLUMNS = [
    "交易日期",
    "市场交易指数",
    "开盘价",
    "最高价",
    "最低价",
    "成交均价",
    "收盘价",
    "成交量",
    "成交额",
]

_ENERGY_CARBON_GZ_COLUMNS = [
    "日期",
    "品种",
    "开盘价",
    "收盘价",
    "最高价",
    "最低价",
    "涨跌",
    "涨跌幅",
    "成交数量",
    "成交金额",
]

_ENERGY_CARBON_HB_COLUMNS = [
    "日期",
    "成交价",
    "成交量",
    "最新",
    "涨跌",
]

_ENERGY_CARBON_DOMESTIC_COLUMNS = [
    "日期",
    "成交价",
    "成交量",
    "成交额",
    "地点",
]

_ENERGY_CARBON_BJ_COLUMNS = [
    "日期",
    "成交量",
    "成交均价",
    "成交额",
    "成交单位",
]


def _empty_energy_carbon_eu() -> pd.DataFrame:
    return pd.DataFrame(columns=_ENERGY_CARBON_EU_COLUMNS)


def _empty_energy_carbon_sz() -> pd.DataFrame:
    return pd.DataFrame(columns=_ENERGY_CARBON_EU_COLUMNS)


def _empty_energy_carbon_gz() -> pd.DataFrame:
    return pd.DataFrame(columns=_ENERGY_CARBON_GZ_COLUMNS)


def _empty_energy_carbon_hb() -> pd.DataFrame:
    return pd.DataFrame(columns=_ENERGY_CARBON_HB_COLUMNS)


def _empty_energy_carbon_domestic() -> pd.DataFrame:
    return pd.DataFrame(columns=_ENERGY_CARBON_DOMESTIC_COLUMNS)


def _empty_energy_carbon_bj() -> pd.DataFrame:
    return pd.DataFrame(columns=_ENERGY_CARBON_BJ_COLUMNS)


def energy_carbon_domestic(symbol: str = "湖北") -> pd.DataFrame:
    """
    碳交易网-行情信息
    http://www.tanjiaoyi.com/
    :param symbol: choice of {'湖北', '上海', '北京', '重庆', '广东', '天津', '深圳', '福建'}
    :type symbol: str
    :return: 行情信息
    :rtype: pandas.DataFrame
    """
    url = "http://k.tanjiaoyi.com:8080/KDataController/getHouseDatasInAverage.do"
    params = {
        "lcnK": "53f75bfcefff58e4046ccfa42171636c",
        "brand": "TAN",
    }
    try:
        r = requests.get(url, params=params, timeout=15)
    except requests.RequestException:
        return _empty_energy_carbon_domestic()
    if r.status_code != 200:
        return _empty_energy_carbon_domestic()
    data_text = r.text
    try:
        data_json = demjson.decode(data_text[data_text.find("(") + 1 : -1])
        temp_df = pd.DataFrame(data_json[symbol])
    except Exception:
        return _empty_energy_carbon_domestic()
    if temp_df.empty:
        return _empty_energy_carbon_domestic()
    try:
        temp_df.columns = [
            "成交价",
            "_",
            "成交量",
            "地点",
            "成交额",
            "日期",
            "_",
        ]
        temp_df = temp_df[_ENERGY_CARBON_DOMESTIC_COLUMNS]
    except (KeyError, ValueError):
        return _empty_energy_carbon_domestic()
    temp_df["日期"] = pd.to_datetime(temp_df["日期"], errors="coerce").dt.date
    temp_df["成交价"] = pd.to_numeric(temp_df["成交价"], errors="coerce")
    temp_df["成交量"] = pd.to_numeric(temp_df["成交量"], errors="coerce")
    temp_df["成交额"] = pd.to_numeric(temp_df["成交额"], errors="coerce")
    return temp_df


def energy_carbon_bj() -> pd.DataFrame:
    """
    北京市碳排放权电子交易平台-北京市碳排放权公开交易行情
    https://www.bjets.com.cn/article/jyxx/
    :return: 北京市碳排放权公开交易行情
    :rtype: pandas.DataFrame
    """
    url = "https://www.bjets.com.cn/article/jyxx/"
    try:
        r = requests.get(url, verify=False, headers=headers, timeout=15)
    except requests.RequestException:
        return _empty_energy_carbon_bj()
    if r.status_code != 200:
        return _empty_energy_carbon_bj()
    soup = BeautifulSoup(r.text, features="lxml")
    table = soup.find("table")
    if table is None or table.find("script") is None:
        return _empty_energy_carbon_bj()
    try:
        total_page = (
            table.find("script")
            .string.split("=")[-1]
            .strip()
            .strip(";")
            .strip('"')
        )
        total_page = int(total_page)
    except (AttributeError, ValueError):
        return _empty_energy_carbon_bj()
    temp_df = pd.DataFrame()
    for i in tqdm(
        range(1, total_page + 1),
        desc="Please wait for a moment",
        leave=False,
    ):
        if i == 1:
            i = ""
        url = f"https://www.bjets.com.cn/article/jyxx/?{i}"
        try:
            r = requests.get(url, verify=False, headers=headers, timeout=15)
        except requests.RequestException:
            return _empty_energy_carbon_bj()
        if r.status_code != 200:
            return _empty_energy_carbon_bj()
        r.encoding = "utf-8"
        try:
            df = pd.read_html(StringIO(r.text))[0]
        except (ValueError, IndexError):
            return _empty_energy_carbon_bj()
        temp_df = pd.concat(objs=[temp_df, df], ignore_index=True)
    if temp_df.empty:
        return _empty_energy_carbon_bj()
    try:
        temp_df.columns = ["日期", "成交量", "成交均价", "成交额"]
    except ValueError:
        return _empty_energy_carbon_bj()
    temp_df["成交单位"] = (
        temp_df["成交额"]
        .str.split("(", expand=True)
        .iloc[:, 1]
        .str.split("）", expand=True)
        .iloc[:, 0]
        .str.split(")", expand=True)
        .iloc[:, 0]
    )
    temp_df["成交额"] = (
        temp_df["成交额"]
        .str.split("(", expand=True)
        .iloc[:, 0]
        .str.split("（", expand=True)
        .iloc[:, 0]
    )
    temp_df["成交量"] = pd.to_numeric(temp_df["成交量"], errors="coerce")
    temp_df["成交均价"] = pd.to_numeric(temp_df["成交均价"], errors="coerce")
    temp_df["成交额"] = temp_df["成交额"].str.replace(",", "")
    temp_df["成交额"] = pd.to_numeric(temp_df["成交额"], errors="coerce")
    temp_df["日期"] = pd.to_datetime(temp_df["日期"], errors="coerce").dt.date
    temp_df.sort_values(by="日期", inplace=True)
    temp_df.reset_index(inplace=True, drop=True)
    return temp_df


def energy_carbon_sz() -> pd.DataFrame:
    """
    深圳碳排放交易所-国内碳情
    http://www.cerx.cn/dailynewsCN/index.htm
    :return: 国内碳情每日行情数据
    :rtype: pandas.DataFrame
    """
    url = "http://www.cerx.cn/dailynewsCN/index.htm"
    try:
        r = requests.get(url, headers=headers, timeout=15)
    except requests.RequestException:
        return _empty_energy_carbon_sz()
    if r.status_code != 200:
        return _empty_energy_carbon_sz()
    soup = BeautifulSoup(r.text, features="lxml")
    pagebar = soup.find(attrs={"class": "pagebar"})
    if pagebar is None or not pagebar.find_all("option"):
        page_num = 1
    else:
        page_num = int(pagebar.find_all("option")[-1].text)
    try:
        big_df = pd.read_html(StringIO(r.text), header=0)[0]
    except ValueError:
        return _empty_energy_carbon_sz()
    for page in tqdm(
        range(2, page_num + 1), desc="Please wait for a moment", leave=False
    ):
        url = f"http://www.cerx.cn/dailynewsCN/index_{page}.htm"
        try:
            r = requests.get(url, headers=headers, timeout=15)
        except requests.RequestException:
            return _empty_energy_carbon_sz()
        if r.status_code != 200:
            return _empty_energy_carbon_sz()
        try:
            temp_df = pd.read_html(StringIO(r.text), header=0)[0]
        except ValueError:
            return _empty_energy_carbon_sz()
        big_df = pd.concat(objs=[big_df, temp_df], ignore_index=True)
    for column in _ENERGY_CARBON_EU_COLUMNS:
        if column not in big_df.columns:
            return _empty_energy_carbon_sz()
    big_df["交易日期"] = pd.to_datetime(big_df["交易日期"], errors="coerce").dt.date
    big_df["开盘价"] = pd.to_numeric(big_df["开盘价"], errors="coerce")
    big_df["最高价"] = pd.to_numeric(big_df["最高价"], errors="coerce")
    big_df["最低价"] = pd.to_numeric(big_df["最低价"], errors="coerce")
    big_df["成交均价"] = pd.to_numeric(big_df["成交均价"], errors="coerce")
    big_df["收盘价"] = pd.to_numeric(big_df["收盘价"], errors="coerce")
    big_df["成交量"] = pd.to_numeric(big_df["成交量"], errors="coerce")
    big_df["成交额"] = pd.to_numeric(big_df["成交额"], errors="coerce")
    big_df.sort_values(by="交易日期", inplace=True)
    big_df.reset_index(inplace=True, drop=True)
    return big_df


def energy_carbon_eu() -> pd.DataFrame:
    """
    深圳碳排放交易所-国际碳情
    http://www.cerx.cn/dailynewsOuter/index.htm
    :return: 国际碳情每日行情数据
    :rtype: pandas.DataFrame
    """
    url = "http://www.cerx.cn/dailynewsOuter/index.htm"
    try:
        r = requests.get(url, headers=headers, timeout=15)
    except requests.RequestException:
        return _empty_energy_carbon_eu()
    if r.status_code != 200:
        return _empty_energy_carbon_eu()
    soup = BeautifulSoup(r.text, features="lxml")
    try:
        big_df = pd.read_html(StringIO(r.text), header=0)[0]
    except ValueError:
        return _empty_energy_carbon_eu()
    pagebar = soup.find(attrs={"class": "pagebar"})
    if pagebar is None or not pagebar.find_all("option"):
        page_num = 1
    else:
        page_num = int(pagebar.find_all("option")[-1].text)
    for page in tqdm(
        range(2, page_num + 1), desc="Please wait for a moment", leave=False
    ):
        url = f"http://www.cerx.cn/dailynewsOuter/index_{page}.htm"
        try:
            r = requests.get(url, headers=headers, timeout=15)
        except requests.RequestException:
            return _empty_energy_carbon_eu()
        if r.status_code != 200:
            return _empty_energy_carbon_eu()
        try:
            temp_df = pd.read_html(StringIO(r.text), header=0)[0]
        except ValueError:
            return _empty_energy_carbon_eu()
        big_df = pd.concat(objs=[big_df, temp_df], ignore_index=True)
    for column in _ENERGY_CARBON_EU_COLUMNS:
        if column not in big_df.columns:
            return _empty_energy_carbon_eu()
    big_df["交易日期"] = pd.to_datetime(big_df["交易日期"], errors="coerce").dt.date
    big_df["开盘价"] = pd.to_numeric(big_df["开盘价"], errors="coerce")
    big_df["最高价"] = pd.to_numeric(big_df["最高价"], errors="coerce")
    big_df["最低价"] = pd.to_numeric(big_df["最低价"], errors="coerce")
    big_df["成交均价"] = pd.to_numeric(big_df["成交均价"], errors="coerce")
    big_df["收盘价"] = pd.to_numeric(big_df["收盘价"], errors="coerce")
    big_df["成交量"] = pd.to_numeric(big_df["成交量"], errors="coerce")
    big_df["成交额"] = pd.to_numeric(big_df["成交额"], errors="coerce")
    big_df.sort_values(by="交易日期", inplace=True)
    big_df.reset_index(inplace=True, drop=True)
    return big_df[_ENERGY_CARBON_EU_COLUMNS]


def energy_carbon_hb() -> pd.DataFrame:
    """
    湖北碳排放权交易中心-现货交易数据-配额-每日概况
    http://www.hbets.cn/list/13.html?page=42
    :return: 现货交易数据-配额-每日概况行情数据
    :rtype: pandas.DataFrame
    """
    url = "https://www.hbets.cn/"
    try:
        r = requests.get(url, headers=headers, timeout=15)
    except requests.RequestException:
        return _empty_energy_carbon_hb()
    if r.status_code != 200:
        return _empty_energy_carbon_hb()
    soup = BeautifulSoup(r.text, features="lxml")
    container = soup.find(name="div", attrs={"class": "threeLeft"})
    if container is None or len(container.find_all("script")) < 2:
        return _empty_energy_carbon_hb()
    data_text = container.find_all("script")[1].text
    start_pos = data_text.find("cjj = '[") + 7  # 找到 JSON 数组开始的位置
    end_pos = data_text.rfind("cjj =") - 31  # 找到 JSON 数组结束的位置
    if start_pos < 7 or end_pos <= start_pos:
        return _empty_energy_carbon_hb()
    try:
        data_json = demjson.decode(data_text[start_pos:end_pos])
    except Exception:
        return _empty_energy_carbon_hb()
    temp_df = pd.DataFrame.from_dict(data_json)
    if temp_df.empty:
        return _empty_energy_carbon_hb()
    temp_df.rename(
        columns={
            "riqi": "日期",
            "cjj": "成交价",
            "cjl": "成交量",
            "zx": "最新",
            "zd": "涨跌",
        },
        inplace=True,
    )
    for column in _ENERGY_CARBON_HB_COLUMNS:
        if column not in temp_df.columns:
            return _empty_energy_carbon_hb()
    temp_df = temp_df[_ENERGY_CARBON_HB_COLUMNS]
    temp_df["日期"] = pd.to_datetime(temp_df["日期"], errors="coerce").dt.date
    temp_df["成交价"] = pd.to_numeric(temp_df["成交价"], errors="coerce")
    temp_df["成交量"] = pd.to_numeric(temp_df["成交量"], errors="coerce")
    temp_df["最新"] = pd.to_numeric(temp_df["最新"], errors="coerce")
    temp_df["涨跌"] = pd.to_numeric(temp_df["涨跌"], errors="coerce")
    return temp_df


def energy_carbon_gz() -> pd.DataFrame:
    """
    广州碳排放权交易中心-行情信息
    http://www.cnemission.com/article/hqxx/
    :return: 行情信息数据
    :rtype: pandas.DataFrame
    """
    url = "http://ets.cnemission.com/carbon/portalIndex/markethistory"
    params = {
        "Top": "1",
        "beginTime": "2010-01-01",
        "endTime": "2030-09-12",
    }
    try:
        r = requests.get(url, params=params, timeout=15)
    except requests.RequestException:
        return _empty_energy_carbon_gz()
    if r.status_code != 200:
        return _empty_energy_carbon_gz()
    try:
        temp_df = pd.read_html(StringIO(r.text), header=0)[1]
        temp_df.columns = _ENERGY_CARBON_GZ_COLUMNS
    except (ValueError, IndexError):
        return _empty_energy_carbon_gz()
    temp_df["日期"] = pd.to_datetime(
        temp_df["日期"], format="%Y%m%d", errors="coerce"
    ).dt.date
    temp_df["开盘价"] = pd.to_numeric(temp_df["开盘价"], errors="coerce")
    temp_df["收盘价"] = pd.to_numeric(temp_df["收盘价"], errors="coerce")
    temp_df["最高价"] = pd.to_numeric(temp_df["最高价"], errors="coerce")
    temp_df["最低价"] = pd.to_numeric(temp_df["最低价"], errors="coerce")
    temp_df["涨跌"] = pd.to_numeric(temp_df["涨跌"], errors="coerce")
    temp_df["涨跌幅"] = temp_df["涨跌幅"].str.strip("%")
    temp_df["涨跌幅"] = pd.to_numeric(temp_df["涨跌幅"], errors="coerce")
    temp_df["成交数量"] = pd.to_numeric(temp_df["成交数量"], errors="coerce")
    temp_df["成交金额"] = pd.to_numeric(temp_df["成交金额"], errors="coerce")
    temp_df.sort_values(by="日期", inplace=True)
    temp_df.reset_index(inplace=True, drop=True)
    return temp_df


if __name__ == "__main__":
    energy_carbon_domestic_df = energy_carbon_domestic(symbol="湖北")
    print(energy_carbon_domestic_df)

    energy_carbon_domestic_df = energy_carbon_domestic(symbol="深圳")
    print(energy_carbon_domestic_df)

    energy_carbon_bj_df = energy_carbon_bj()
    print(energy_carbon_bj_df)

    energy_carbon_sz_df = energy_carbon_sz()
    print(energy_carbon_sz_df)

    energy_carbon_eu_df = energy_carbon_eu()
    print(energy_carbon_eu_df)

    energy_carbon_hb_df = energy_carbon_hb()
    print(energy_carbon_hb_df)

    energy_carbon_gz_df = energy_carbon_gz()
    print(energy_carbon_gz_df)
