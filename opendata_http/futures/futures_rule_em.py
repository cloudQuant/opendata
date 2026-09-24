#!/usr/bin/env python
# -*- coding:utf-8 -*-
# Ported from akshare (https://github.com/cloudQuant/akshare.git) @ c4f6a631c259783dbc2507b6b27d179b3e88079d
# Copyright (c) 2019-2026 Albert King — MIT License (see THIRD_PARTY_NOTICES.md)
"""
Date: 2024/1/17 19:30
Desc: 东方财富网-期货行情-品种及交易规则
https://portal.eastmoneyfutures.com/pages/service/jyts.html#jyrl
"""

import pandas as pd
import requests
from opendata_http.utils.cons import headers


def futures_rule_em() -> pd.DataFrame:
    """
    东方财富网-期货行情-品种及交易规则
    https://portal.eastmoneyfutures.com/pages/service/jyts.html#jyrl
    :return: 品种及交易规则
    :rtype: pandas.DataFrame
    """
    url = "https://eastmoneyfutures.com/api/ComManage/GetPZJYInfo"
    r = requests.get(url, headers=headers)
    data_json = r.json()
    temp_df = pd.DataFrame(data_json["Data"])
    return temp_df


def futures_trading_hours_em():
    """
    东方财富网-期货交易时间
    https://qhweb.eastmoney.com/tradinghours
    """
    pass


if __name__ == "__main__":
    futures_rule_em_df = futures_rule_em()
    print(futures_rule_em_df)
