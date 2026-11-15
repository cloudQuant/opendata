#!/usr/bin/env python
# -*- coding:utf-8 -*-
"""
Date: 2021/11/15 20:40
Desc: 艺恩
视频放映
电视剧集
综艺节目
https://www.endata.com.cn/Video/index.html
"""

import json
import os

import pandas as pd
import requests
import py_mini_racer


_VIDEO_YIEN_COLUMNS = [
    "排序",
    "名称",
    "类型",
    "播映指数",
    "媒体热度",
    "用户热度",
    "好评度",
    "观看度",
    "统计日期",
]


def _empty_video_yien() -> pd.DataFrame:
    return pd.DataFrame(columns=_VIDEO_YIEN_COLUMNS)


def _get_js_path(name: str = "", module_file: str = "") -> str:
    """
    get JS file path
    :param name: file name
    :type name: str
    :param module_file: filename
    :type module_file: str
    :return: 路径
    :rtype: str
    """
    module_folder = os.path.abspath(os.path.dirname(os.path.dirname(module_file)))
    module_json_path = os.path.join(module_folder, "movie", name)
    return module_json_path


def _get_file_content(file_name: str = "jm.js"):
    """
    read the file content
    :param file_name: filename
    :type file_name: str
    :return: file content
    :rtype: str
    """
    setting_file_name = file_name
    setting_file_path = _get_js_path(setting_file_name, __file__)
    with open(setting_file_path) as f:
        file_data = f.read()
    return file_data


def decrypt(origin_data: str = "") -> str:
    """
    解密艺恩的加密数据
    :param origin_data: 解密前的字符串
    :type origin_data: str
    :return: 解密后的字符串
    :rtype: str
    """
    file_data = _get_file_content(file_name="jm.js")
    ctx = py_mini_racer.MiniRacer()
    ctx.eval(file_data)
    data = ctx.call("webInstace.shell", origin_data)
    return data


def _post_endata_video_json(url: str, payload: dict) -> dict:
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        ),
        "Referer": "https://www.endata.com.cn/Video/index.html",
        "Origin": "https://www.endata.com.cn",
    }
    r = requests.post(url, data=payload, headers=headers, timeout=15)
    r.encoding = "utf8"
    if r.status_code != 200:
        return {}
    try:
        return json.loads(decrypt(r.text))
    except json.JSONDecodeError:
        return {}


def video_tv() -> pd.DataFrame:
    """
    艺恩-视频放映-电视剧集
    https://www.endata.com.cn/Video/index.html
    :return: 电视剧集
    :rtype: pandas.DataFrame
    """
    url = "https://www.endata.com.cn/API/GetData.ashx"
    payload = {"tvType": 2, "MethodName": "BoxOffice_GetTvData_PlayIndexRank"}
    data_json = _post_endata_video_json(url, payload)
    data = data_json.get("Data") or {}
    if not data.get("Table"):
        return _empty_video_yien()
    temp_df = pd.DataFrame(data["Table"])
    report_date = (data.get("Table1") or [{}])[0].get("MaxDate")
    temp_df.columns = [
        "排序",
        "名称",
        "类型",
        "播映指数",
        "用户热度",
        "媒体热度",
        "观看度",
        "好评度",
    ]
    temp_df = temp_df[
        ["排序", "名称", "类型", "播映指数", "媒体热度", "用户热度", "好评度", "观看度"]
    ]
    temp_df["统计日期"] = report_date
    return temp_df


def video_variety_show() -> pd.DataFrame:
    """
    艺恩-视频放映-综艺节目
    https://www.endata.com.cn/Video/index.html
    :return: 综艺节目
    :rtype: pandas.DataFrame
    """
    url = "https://www.endata.com.cn/API/GetData.ashx"
    payload = {"tvType": 8, "MethodName": "BoxOffice_GetTvData_PlayIndexRank"}
    data_json = _post_endata_video_json(url, payload)
    data = data_json.get("Data") or {}
    if not data.get("Table"):
        return _empty_video_yien()
    temp_df = pd.DataFrame(data["Table"])
    report_date = (data.get("Table1") or [{}])[0].get("MaxDate")
    temp_df.columns = [
        "排序",
        "名称",
        "类型",
        "播映指数",
        "用户热度",
        "媒体热度",
        "观看度",
        "好评度",
    ]
    temp_df = temp_df[
        ["排序", "名称", "类型", "播映指数", "媒体热度", "用户热度", "好评度", "观看度"]
    ]
    temp_df["统计日期"] = report_date
    return temp_df


if __name__ == "__main__":
    video_tv_df = video_tv()
    print(video_tv_df)

    video_variety_show_df = video_variety_show()
    print(video_variety_show_df)
