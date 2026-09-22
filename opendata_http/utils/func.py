# Ported from akshare (https://github.com/cloudQuant/akshare.git) @ c4f6a631c259783dbc2507b6b27d179b3e88079d
# Copyright (c) 2019-2026 Albert King — MIT License (see THIRD_PARTY_NOTICES.md)
# !/usr/bin/env python
"""
Date: 2025/3/10 18:00
Desc: 通用帮助函数
"""

import math
import random
import ssl
import time
from typing import List, Dict

import pandas as pd
import requests
import urllib3
from requests.adapters import HTTPAdapter
from urllib3.util.ssl_ import create_urllib3_context

from opendata_http.utils.request import eastmoney_fallback_urls, request_eastmoney
from opendata_http.utils.tqdm import get_tqdm

# 禁用SSL警告
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


class SSLAdapter(HTTPAdapter):
    """自定义SSL适配器，解决SSL握手问题"""
    def init_poolmanager(self, *args, **kwargs):
        ctx = create_urllib3_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        ctx.set_ciphers('DEFAULT@SECLEVEL=1')
        kwargs['ssl_context'] = ctx
        return super().init_poolmanager(*args, **kwargs)


def _get_ssl_session():
    """创建带有自定义SSL处理的session"""
    session = requests.Session()
    adapter = SSLAdapter()
    session.mount('http://', adapter)
    session.mount('https://', adapter)
    return session


def _request_with_ssl_fallback(url: str, params: Dict, timeout: int):
    try:
        return request_eastmoney(url, params=params, timeout=timeout)
    except (requests.RequestException, ValueError):
        session = _get_ssl_session()
        last_exception = None
        for candidate_url in eastmoney_fallback_urls(url):
            try:
                return session.get(
                    candidate_url,
                    params=params,
                    timeout=timeout,
                    verify=False,
                )
            except requests.RequestException as exc:
                last_exception = exc
        raise RuntimeError(
            f"Eastmoney paginated endpoint request failed: {url}"
        ) from last_exception


def fetch_paginated_data(url: str, base_params: Dict, timeout: int = 15):
    """
    东方财富-分页获取数据并合并结果
    https://quote.eastmoney.com/f1.html?newcode=0.000001
    :param url: 股票代码
    :type url: str
    :param base_params: 基础请求参数
    :type base_params: dict
    :param timeout: 请求超时时间
    :type timeout: str
    :return: 合并后的数据
    :rtype: pandas.DataFrame
    """
    # 复制参数以避免修改原始参数
    params = base_params.copy()
    # 获取第一页数据，用于确定分页信息
    r = _request_with_ssl_fallback(url, params=params, timeout=timeout)
    if r.status_code != 200:
        raise RuntimeError(
            f"Eastmoney paginated endpoint returned HTTP {r.status_code}: {url}"
        )
    try:
        data_json = r.json()
    except ValueError as exc:
        raise RuntimeError(
            f"Eastmoney paginated endpoint returned non-JSON response: {url}; "
            f"preview={r.text[:120]!r}"
        ) from exc
    data = data_json.get("data") or {}
    diff = data.get("diff") or []
    if not diff:
        return pd.DataFrame()
    # 计算分页信息
    per_page_num = len(diff)
    total_page = math.ceil(data.get("total", 0) / per_page_num)
    # 存储所有页面数据
    temp_list = []
    # 添加第一页数据
    temp_list.append(pd.DataFrame(diff))
    # 获取进度条
    tqdm = get_tqdm()
    # 获取剩余页面数据
    for page in tqdm(range(2, total_page + 1), leave=False):
        params.update({"pn": page})
        # 添加随机延迟，避免请求过于频繁
        time.sleep(random.uniform(0.5, 1.5))
        r = _request_with_ssl_fallback(url, params=params, timeout=timeout)
        if r.status_code != 200:
            raise RuntimeError(
                f"Eastmoney paginated endpoint returned HTTP {r.status_code}: {url}"
            )
        try:
            data_json = r.json()
        except ValueError as exc:
            raise RuntimeError(
                f"Eastmoney paginated endpoint returned non-JSON response: {url}; "
                f"preview={r.text[:120]!r}"
            ) from exc
        inner_temp_df = pd.DataFrame((data_json.get("data") or {}).get("diff") or [])
        temp_list.append(inner_temp_df)
    # 合并所有数据
    temp_df = pd.concat(temp_list, ignore_index=True)
    temp_df["f3"] = pd.to_numeric(temp_df["f3"], errors="coerce")
    temp_df.sort_values(by=["f3"], ascending=False, inplace=True, ignore_index=True)
    temp_df.reset_index(inplace=True)
    temp_df["index"] = temp_df["index"].astype(int) + 1
    return temp_df


def set_df_columns(df: pd.DataFrame, cols: List[str]) -> pd.DataFrame:
    """
    设置 pandas.DataFrame 为空的情况
    :param df: 需要设置命名的数据框
    :type df: pandas.DataFrame
    :param cols: 字段的列表
    :type cols: list
    :return: 重新设置后的数据
    :rtype: pandas.DataFrame
    """
    if df.shape == (0, 0):
        return pd.DataFrame(data=[], columns=cols)
    else:
        df.columns = cols
        return df
