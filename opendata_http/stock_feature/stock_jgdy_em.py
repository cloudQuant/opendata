#!/usr/bin/env python
# -*- coding:utf-8 -*-
# Ported from akshare (https://github.com/cloudQuant/akshare.git) @ c4f6a631c259783dbc2507b6b27d179b3e88079d
# Copyright (c) 2019-2026 Albert King — MIT License (see THIRD_PARTY_NOTICES.md)
"""
date: 2022/2/14 20:02
desc: 东方财富网-数据中心-特色数据-机构调研
http://data.eastmoney.com/jgdy/
东方财富网-数据中心-特色数据-机构调研-机构调研统计: http://data.eastmoney.com/jgdy/tj.html
东方财富网-数据中心-特色数据-机构调研-机构调研详细: http://data.eastmoney.com/jgdy/xx.html
"""

import pandas as pd
import requests
import ssl
import urllib3
from requests.adapters import HTTPAdapter
from urllib3.util.ssl_ import create_urllib3_context
from tqdm import tqdm


urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


class SSLAdapter(HTTPAdapter):
    def init_poolmanager(self, *args, **kwargs):
        ctx = create_urllib3_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        ctx.set_ciphers('DEFAULT@SECLEVEL=1')
        kwargs['ssl_context'] = ctx
        return super().init_poolmanager(*args, **kwargs)


def _get_ssl_session():
    session = requests.Session()
    session.mount('https://', SSLAdapter())
    return session


def stock_jgdy_tj_em(date: str = "20220101") -> pd.DataFrame:
    """
    东方财富网-数据中心-特色数据-机构调研-机构调研统计
    https://data.eastmoney.com/jgdy/tj.html
    :param date: 开始时间
    :type date: str
    :return: 机构调研统计
    :rtype: pandas.DataFrame
    """
    url = "https://datacenter-web.eastmoney.com/api/data/v1/get"
    params = {
        "sortColumns": "NOTICE_DATE,SUM,RECEIVE_START_DATE,SECURITY_CODE",
        "sortTypes": "-1,-1,-1,1",
        "pageSize": "500",
        "pageNumber": "1",
        "reportName": "RPT_ORG_SURVEYNEW",
        "columns": "ALL",
        "quoteColumns": "f2~01~SECURITY_CODE~CLOSE_PRICE,f3~01~SECURITY_CODE~CHANGE_RATE",
        "source": "WEB",
        "client": "WEB",
        "filter": f"""(NUMBERNEW="1")(IS_SOURCE="1")(NOTICE_DATE>'{"-".join([date[:4], date[4:6], date[6:]])}')""",
    }
    r = requests.get(url, params=params)
    data_json = r.json()
    total_page = data_json["result"]["pages"]
    big_df = pd.DataFrame()
    for page in tqdm(range(1, total_page + 1), leave=False):
        params.update({"pageNumber": page})
        r = requests.get(url, params=params)
        data_json = r.json()
        temp_df = pd.DataFrame(data_json["result"]["data"])
        big_df = pd.concat([big_df, temp_df])
    big_df.reset_index(inplace=True)
    big_df["index"] = list(range(1, len(big_df) + 1))
    big_df.columns = [
        "序号",
        "_",
        "代码",
        "名称",
        "_",
        "公告日期",
        "接待日期",
        "_",
        "_",
        "_",
        "_",
        "_",
        "_",
        "_",
        "接待地点",
        "_",
        "接待方式",
        "_",
        "接待人员",
        "_",
        "_",
        "_",
        "_",
        "_",
        "接待机构数量",
        "_",
        "_",
        "_",
        "_",
        "_",
        "_",
        "最新价",
        "涨跌幅",
    ]
    big_df = big_df[
        [
            "序号",
            "代码",
            "名称",
            "最新价",
            "涨跌幅",
            "接待机构数量",
            "接待方式",
            "接待人员",
            "接待地点",
            "接待日期",
            "公告日期",
        ]
    ]
    big_df["最新价"] = pd.to_numeric(big_df["最新价"], errors="coerce")
    big_df["涨跌幅"] = pd.to_numeric(big_df["涨跌幅"], errors="coerce")
    big_df["接待机构数量"] = pd.to_numeric(big_df["接待机构数量"], errors="coerce")
    big_df["接待日期"] = pd.to_datetime(big_df["接待日期"], errors="coerce").dt.date
    big_df["公告日期"] = pd.to_datetime(big_df["公告日期"], errors="coerce").dt.date
    return big_df


def stock_jgdy_detail_em(date: str = "20241211", max_pages: int = None) -> pd.DataFrame:
    """
    东方财富网-数据中心-特色数据-机构调研-机构调研详细
    https://data.eastmoney.com/jgdy/xx.html
    :param date: 开始时间
    :type date: str
    :return: 机构调研详细
    :rtype: pandas.DataFrame
    """
    url = "https://datacenter-web.eastmoney.com/api/data/v1/get"
    params = {
        "sortColumns": "NOTICE_DATE,RECEIVE_START_DATE,SECURITY_CODE,NUMBERNEW",
        "sortTypes": "-1,-1,1,-1",
        "pageSize": "50",
        "pageNumber": "1",
        "reportName": "RPT_ORG_SURVEY",
        "columns": "SECUCODE,SECURITY_CODE,SECURITY_NAME_ABBR,NOTICE_DATE,RECEIVE_START_DATE,"
        "RECEIVE_OBJECT,RECEIVE_PLACE,RECEIVE_WAY_EXPLAIN,INVESTIGATORS,RECEPTIONIST,ORG_TYPE",
        "quoteColumns": "f2~01~SECURITY_CODE~CLOSE_PRICE,f3~01~SECURITY_CODE~CHANGE_RATE",
        "quoteType": "0",
        "source": "WEB",
        "client": "WEB",
        "filter": f"""(IS_SOURCE="1")(RECEIVE_START_DATE>'{"-".join([date[:4], date[4:6], date[6:]])}')""",
    }
    output_columns = [
        "序号",
        "代码",
        "名称",
        "最新价",
        "涨跌幅",
        "调研机构",
        "机构类型",
        "调研人员",
        "接待方式",
        "接待人员",
        "接待地点",
        "调研日期",
        "公告日期",
    ]
    r = requests.get(url, params=params, timeout=15)
    r.raise_for_status()
    data_json = r.json()
    total_page = int((data_json.get("result") or {}).get("pages") or 0)
    if max_pages is not None:
        total_page = min(total_page, int(max_pages))
    big_df = pd.DataFrame()
    for page in tqdm(range(1, total_page + 1), leave=False):
        params.update({"pageNumber": page})
        r = requests.get(url, params=params, timeout=15)
        r.raise_for_status()
        data_json = r.json()
        records = (data_json.get("result") or {}).get("data") or []
        if not records:
            continue
        temp_df = pd.DataFrame(records)
        big_df = pd.concat([big_df, temp_df])
    if big_df.empty:
        return pd.DataFrame(columns=output_columns)
    big_df.reset_index(inplace=True)
    big_df["index"] = list(range(1, len(big_df) + 1))
    big_df.columns = [
        "序号",
        "_",
        "代码",
        "名称",
        "公告日期",
        "调研日期",
        "调研机构",
        "接待地点",
        "接待方式",
        "调研人员",
        "接待人员",
        "机构类型",
        "最新价",
        "涨跌幅",
    ]
    big_df = big_df[output_columns]
    big_df["最新价"] = pd.to_numeric(big_df["最新价"], errors="coerce")
    big_df["涨跌幅"] = pd.to_numeric(big_df["涨跌幅"], errors="coerce")
    big_df["调研日期"] = pd.to_datetime(big_df["调研日期"], errors="coerce").dt.date
    big_df["公告日期"] = pd.to_datetime(big_df["公告日期"], errors="coerce").dt.date
    return big_df


if __name__ == "__main__":
    stock_jgdy_tj_em_df = stock_jgdy_tj_em(date="20180928")
    print(stock_jgdy_tj_em_df)

    stock_jgdy_detail_em_df = stock_jgdy_detail_em(date="20210915")
    print(stock_jgdy_detail_em_df)
