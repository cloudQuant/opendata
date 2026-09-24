#!/usr/bin/env python
# -*- coding:utf-8 -*-
# Ported from akshare (https://github.com/cloudQuant/akshare.git) @ c4f6a631c259783dbc2507b6b27d179b3e88079d
# Copyright (c) 2019-2026 Albert King — MIT License (see THIRD_PARTY_NOTICES.md)
"""
Date: 2019/12/30 21:02
Desc:
"""

stock_em_sy_js = """
    function getCode(num) {
            var str = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz";
            var codes = str.split('');
            num = num || 6;
            var code = "";
            for (var i = 0; i < num; i++) {
                code += codes[Math.floor(Math.random() * 52)]
            }
            return code
        }
    """
