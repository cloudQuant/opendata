# -*- coding:utf-8 -*-
# Ported from akshare (https://github.com/cloudQuant/akshare.git) @ c4f6a631c259783dbc2507b6b27d179b3e88079d
# Copyright (c) 2019-2026 Albert King — MIT License (see THIRD_PARTY_NOTICES.md)
# !/usr/bin/env python
"""
Date: 2024/12/30 15:30
Desc: 导入文件工具，可以正确处理路径问题
"""
import os

import pathlib
from importlib import resources


def get_ths_js(file: str = "ths.js") -> pathlib.Path:
    """
    get path to data "ths.js" text file.
    :return: 文件路径
    :rtype: pathlib.Path
    """
    raise RuntimeError(
        f"resource {file!r} is unavailable: the upstream akshare.data "
        "package never existed (A2.3); see docs/port-report.md"
    )


def get_crypto_info_csv(file: str = "crypto_info.zip") -> pathlib.Path:
    """
    get path to data "ths.js" text file.
    :return: 文件路径
    :rtype: pathlib.Path
    """
    raise RuntimeError(
        f"resource {file!r} is unavailable: the upstream akshare.data "
        "package never existed (A2.3); see docs/port-report.md"
    )


if __name__ == "__main__":
    get_ths_js_path = get_ths_js(file="ths.js")
    print(get_ths_js_path)

    get_crypto_info_csv_path = get_crypto_info_csv(file="crypto_info.zip")
    print(get_crypto_info_csv_path)
