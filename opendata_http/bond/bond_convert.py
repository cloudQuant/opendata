#!/usr/bin/env python
# -*- coding:utf-8 -*-
# Ported from akshare (https://github.com/cloudQuant/akshare.git) @ c4f6a631c259783dbc2507b6b27d179b3e88079d
# Copyright (c) 2019-2026 Albert King — MIT License (see THIRD_PARTY_NOTICES.md)
"""
Date: 2025/5/16 19:00
Desc: 债券-集思录-可转债
集思录：https://www.jisilu.cn/data/cbnew/#cb
"""
import os

from io import StringIO
import json
import re
import pandas as pd
import requests
import time

from opendata_http.utils import demjson


def _update_jsl_cookie(cookie: str) -> str:
    """
    动态更新集思录 cookie 中的时间戳字段，避免过期
    :param cookie: 原始 cookie 字符串
    :return: 更新后的 cookie 字符串
    """
    if not cookie:
        return cookie
    
    current_ts = int(time.time())
    
    # 更新 Hm_lpvt_* (最后访问时间戳)
    cookie = re.sub(
        r'(Hm_lpvt_[a-f0-9]+)=\d+',
        rf'\1={current_ts}',
        cookie
    )
    
    # 更新 SERVERID 中间的时间戳: hash|current_ts|first_ts
    def update_serverid(match):
        parts = match.group(1).split('|')
        if len(parts) == 3:
            parts[1] = str(current_ts)
            return f"SERVERID={'|'.join(parts)}"
        return match.group(0)
    
    cookie = re.sub(r'SERVERID=([^;]+)', update_serverid, cookie)
    
    return cookie


def _jsl_fetch_with_playwright(user: str, password: str) -> list:
    """
    使用 Playwright 模拟浏览器登录集思录并获取可转债数据
    :param user: 用户名/手机号
    :param password: 密码
    :return: 可转债数据行列表
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        raise ImportError(
            "使用用户名密码登录需要安装 playwright，请执行:\n"
            "pip install playwright\n"
            "playwright install chromium"
        )
    
    all_rows = []
    
    with sync_playwright() as p:
        # 启动浏览器（无头模式）
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        )
        page = context.new_page()
        
        try:
            # 1. 访问登录页面
            page.goto("https://www.jisilu.cn/account/login/", timeout=30000)
            page.wait_for_load_state("networkidle")
            time.sleep(1)
            
            # 2. 填写登录表单 - 使用正确的选择器
            # 定位到账号密码登录表单中的输入框（第一个表单）
            user_input = page.locator('input[name="user_name"][placeholder="手机号/用户名"]')
            user_input.fill(user)
            
            # 密码输入框（账号密码登录表单中的）
            pwd_input = page.locator('input[name="password"][placeholder="密码"]')
            pwd_input.fill(password)
            
            # 勾选"同意用户协议"等复选框（如果存在且未勾选）
            checkboxes = page.locator('input[type="checkbox"]').all()
            for cb in checkboxes:
                if cb.is_visible() and not cb.is_checked():
                    cb.check()
            
            # 3. 点击登录按钮 - 使用文本匹配（注意：是 .btn 而非 button）
            login_btn = page.locator('.btn:has-text("登录")').first
            login_btn.click()
            
            # 4. 等待登录完成
            time.sleep(5)  # 给足够时间完成登录和跳转
            page.wait_for_load_state("networkidle", timeout=15000)
            
            # 5. 访问可转债数据页面
            page.goto("https://www.jisilu.cn/data/cbnew/#cb", timeout=30000)
            page.wait_for_load_state("networkidle")
            time.sleep(2)  # 等待数据加载
            
            # 6. 使用浏览器内的 fetch 发起 API 请求获取所有数据
            api_url = "https://www.jisilu.cn/data/cbnew/cb_list_new/"
            
            # 分页获取数据
            page_num = 1
            while True:
                # 构造请求参数
                ts = int(time.time() * 1000)
                
                # 使用 page.evaluate 发起 fetch 请求
                response_text = page.evaluate(f"""
                    async () => {{
                        const formData = new URLSearchParams();
                        formData.append('fprice', '');
                        formData.append('tprice', '');
                        formData.append('curr_iss_amt', '');
                        formData.append('volume', '');
                        formData.append('svolume', '');
                        formData.append('premium_rt', '');
                        formData.append('ytm_rt', '');
                        formData.append('market', '');
                        formData.append('rating_cd', '');
                        formData.append('is_search', 'N');
                        formData.append('market_cd[]', 'shmb');
                        formData.append('market_cd[]', 'shkc');
                        formData.append('market_cd[]', 'szmb');
                        formData.append('market_cd[]', 'szcy');
                        formData.append('btype', '');
                        formData.append('listed', 'Y');
                        formData.append('qflag', 'N');
                        formData.append('sw_cd', '');
                        formData.append('bond_ids', '');
                        formData.append('rp', '500');
                        formData.append('page', '{page_num}');
                        
                        const response = await fetch("{api_url}?___jsl=LST___t={ts}", {{
                            method: "POST",
                            headers: {{
                                "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                                "X-Requested-With": "XMLHttpRequest"
                            }},
                            body: formData.toString()
                        }});
                        return await response.text();
                    }}
                """)
                
                data_json = json.loads(response_text)
                rows = data_json.get("rows", [])
                
                if not rows:
                    break
                    
                all_rows.extend(rows)
                
                # 检查是否获取完所有数据
                total_count = data_json.get("total", 0)
                all_count = data_json.get("all", 0)
                
                # 如果有警告说明登录失败
                warn_msg = data_json.get("warn")
                if warn_msg and all_count > len(all_rows):
                    print(f"警告: {warn_msg}")
                    print("登录可能失败，请检查用户名和密码是否正确。")
                    break
                
                if len(all_rows) >= total_count:
                    break
                    
                page_num += 1
                
        finally:
            browser.close()
    
    return all_rows


def bond_cb_index_jsl() -> pd.DataFrame:
    """
    首页-可转债-集思录可转债等权指数
    https://www.jisilu.cn/web/data/cb/index
    :return: 集思录可转债等权指数
    :rtype: pandas.DataFrame
    """
    url = "https://www.jisilu.cn/webapi/cb/index_history/"
    r = requests.get(url)
    data_dict = demjson.decode(r.text)["data"]
    temp_df = pd.DataFrame(data_dict)
    return temp_df


def bond_cb_jsl(
    cookie: str = None,
    user: str = None,
    password: str = None
) -> pd.DataFrame:
    """
    集思录可转债
    https://www.jisilu.cn/data/cbnew/#cb
    :param cookie: 输入获取到的游览器 cookie（与 user/password 二选一）
    :type cookie: str
    :param user: 集思录用户名/手机号（与 cookie 二选一，需配合 password 使用）
    :type user: str
    :param password: 集思录密码（与 cookie 二选一，需配合 user 使用）
    :type password: str
    :return: 集思录可转债
    :rtype: pandas.DataFrame
    """
    # 如果提供了用户名和密码，使用 Playwright 方式获取数据
    if user and password:
        all_rows = _jsl_fetch_with_playwright(user, password)
    else:
        # 使用 cookie 方式获取数据
        url = "https://www.jisilu.cn/data/cbnew/cb_list_new/"
        # 动态更新 cookie 中的时间戳，避免过期
        updated_cookie = _update_jsl_cookie(cookie)
        headers = {
            "accept": "application/json, text/javascript, */*; q=0.01",
            "accept-encoding": "gzip, deflate, br",
            "accept-language": "zh-CN,zh;q=0.9,en;q=0.8",
            "cache-control": "no-cache",
            "content-type": "application/x-www-form-urlencoded; charset=UTF-8",
            "cookie": updated_cookie,
            "origin": "https://www.jisilu.cn",
            "pragma": "no-cache",
            "referer": "https://www.jisilu.cn/data/cbnew/",
            "sec-ch-ua": '" Not;A Brand";v="99", "Google Chrome";v="91", "Chromium";v="91"',
            "sec-ch-ua-mobile": "?0",
            "sec-fetch-dest": "empty",
            "sec-fetch-mode": "cors",
            "sec-fetch-site": "same-origin",
            "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/91.0.4472.164 Safari/537.36",
            "x-requested-with": "XMLHttpRequest",
        }
        params = {
            "___jsl": f"LST___t={int(time.time() * 1000)}",
        }
        # 使用列表形式的 payload 以支持重复键 market_cd[]
        payload = [
            ("fprice", ""),
            ("tprice", ""),
            ("curr_iss_amt", ""),
            ("volume", ""),
            ("svolume", ""),
            ("premium_rt", ""),
            ("ytm_rt", ""),
            ("market", ""),
            ("rating_cd", ""),
            ("is_search", "N"),
            ("market_cd[]", "shmb"),
            ("market_cd[]", "shkc"),
            ("market_cd[]", "szmb"),
            ("market_cd[]", "szcy"),
            ("btype", ""),
            ("listed", "Y"),
            ("qflag", "N"),
            ("sw_cd", ""),
            ("bond_ids", ""),
            ("rp", "5000"),
        ]
        # 分页获取所有数据
        all_rows = []
        page = 1
        while True:
            page_payload = payload + [("page", str(page))]
            r = requests.post(url, params=params, data=page_payload, headers=headers)
            data_json = r.json()
            rows = data_json.get("rows", [])
            if not rows:
                break
            all_rows.extend(rows)
            # 检查是否有登录限制警告
            warn_msg = data_json.get("warn")
            all_count = data_json.get("all", 0)
            if warn_msg and all_count > len(all_rows):
                print(f"警告: {warn_msg}")
                print(f"提示: 总共有 {all_count} 条数据，当前仅获取 {len(all_rows)} 条。")
                print("原因: 登录凭证(kbz__user_login)已在服务器端过期。")
                print("解决: 请使用 user 和 password 参数自动登录，或重新获取 cookie。")
                break
            # 检查是否还有更多数据
            total_count = data_json.get("total", 0)
            if len(all_rows) >= total_count:
                break
            page += 1
    
    # 处理返回数据
    if not all_rows:
        return pd.DataFrame()
    
    temp_df = pd.DataFrame([item["cell"] for item in all_rows])
    temp_df.rename(
        columns={
            "bond_id": "代码",
            "bond_nm": "转债名称",
            "price": "现价",
            "increase_rt": "涨跌幅",
            "stock_id": "正股代码",
            "stock_nm": "正股名称",
            "sprice": "正股价",
            "sincrease_rt": "正股涨跌",
            "pb": "正股PB",
            "convert_price": "转股价",
            "convert_value": "转股价值",
            "premium_rt": "转股溢价率",
            "dblow": "双低",
            "rating_cd": "债券评级",
            "put_convert_price": "回售触发价",
            "force_redeem_price": "强赎触发价",
            "convert_amt_ratio": "转债占比",
            "maturity_dt": "到期时间",
            "year_left": "剩余年限",
            "curr_iss_amt": "剩余规模",
            "volume": "成交额",
            "turnover_rt": "换手率",
            "ytm_rt": "到期税前收益",
        },
        inplace=True,
    )

    temp_df = temp_df[
        [
            "代码",
            "转债名称",
            "现价",
            "涨跌幅",
            "正股代码",
            "正股名称",
            "正股价",
            "正股涨跌",
            "正股PB",
            "转股价",
            "转股价值",
            "转股溢价率",
            "债券评级",
            "回售触发价",
            "强赎触发价",
            "转债占比",
            "到期时间",
            "剩余年限",
            "剩余规模",
            "成交额",
            "换手率",
            "到期税前收益",
            "双低",
        ]
    ]
    temp_df["到期时间"] = pd.to_datetime(temp_df["到期时间"], errors="coerce").dt.date
    temp_df["现价"] = pd.to_numeric(temp_df["现价"], errors="coerce")
    temp_df["涨跌幅"] = pd.to_numeric(temp_df["涨跌幅"], errors="coerce")
    temp_df["正股价"] = pd.to_numeric(temp_df["正股价"], errors="coerce")
    temp_df["正股涨跌"] = pd.to_numeric(temp_df["正股涨跌"], errors="coerce")
    temp_df["正股PB"] = pd.to_numeric(temp_df["正股PB"], errors="coerce")
    temp_df["转股价"] = pd.to_numeric(temp_df["转股价"], errors="coerce")
    temp_df["转股价值"] = pd.to_numeric(temp_df["转股价值"], errors="coerce")
    temp_df["转股溢价率"] = pd.to_numeric(temp_df["转股溢价率"], errors="coerce")
    temp_df["回售触发价"] = pd.to_numeric(temp_df["回售触发价"], errors="coerce")
    temp_df["强赎触发价"] = pd.to_numeric(temp_df["强赎触发价"], errors="coerce")
    temp_df["转债占比"] = pd.to_numeric(temp_df["转债占比"], errors="coerce")
    temp_df["剩余年限"] = pd.to_numeric(temp_df["剩余年限"], errors="coerce")
    temp_df["剩余规模"] = pd.to_numeric(temp_df["剩余规模"], errors="coerce")
    temp_df["成交额"] = pd.to_numeric(temp_df["成交额"], errors="coerce")
    temp_df["换手率"] = pd.to_numeric(temp_df["换手率"], errors="coerce")
    temp_df["到期税前收益"] = pd.to_numeric(temp_df["到期税前收益"], errors="coerce")
    return temp_df


def bond_cb_redeem_jsl() -> pd.DataFrame:
    """
    集思录可转债-强赎
    https://www.jisilu.cn/data/cbnew/#redeem
    :return: 集思录可转债-强赎
    :rtype: pandas.DataFrame
    """
    url = "https://www.jisilu.cn/data/cbnew/redeem_list/"
    headers = {
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "Accept-Encoding": "gzip, deflate, br",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Cache-Control": "no-cache",
        "Connection": "keep-alive",
        "Content-Length": "5",
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        "Host": "www.jisilu.cn",
        "Origin": "https://www.jisilu.cn",
        "Pragma": "no-cache",
        "Referer": "https://www.jisilu.cn/data/cbnew/",
        "sec-ch-ua": '" Not A;Brand";v="99", "Chromium";v="101", "Google Chrome";v="101"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "same-origin",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/101.0.4951.67 Safari/537.36",
        "X-Requested-With": "XMLHttpRequest",
    }
    params = {
        "___jsl": "LST___t=1653394005966",
    }
    payload = {
        "rp": "50",
    }
    r = requests.post(url, params=params, json=payload, headers=headers)
    data_json = r.json()
    temp_df = pd.DataFrame([item["cell"] for item in data_json["rows"]])
    temp_df.rename(
        columns={
            "bond_id": "代码",
            "bond_nm": "名称",
            "price": "现价",
            "stock_id": "正股代码",
            "stock_nm": "正股名称",
            "margin_flg": "-",
            "btype": "-",
            "orig_iss_amt": "规模",
            "curr_iss_amt": "剩余规模",
            "convert_dt": "转股起始日",
            "convert_price": "转股价",
            "next_put_dt": "-",
            "redeem_dt": "-",
            "force_redeem": "-",
            "redeem_flag": "-",
            "redeem_price": "-",
            "redeem_price_ratio": "强赎触发比",
            "real_force_redeem_price": "强赎价",
            "redeem_remain_days": "-",
            "redeem_real_days": "-",
            "redeem_total_days": "-",
            "recount_dt": "-",
            "redeem_count_days": "-",
            "redeem_tc": "强赎条款",
            "sprice": "正股价",
            "delist_dt": "最后交易日",
            "maturity_dt": "到期日",
            "redeem_icon": "强赎状态",
            "redeem_orders": "-",
            "at_maturity": "-",
            "redeem_count": "强赎天计数",
            "after_next_put_dt": "-",
            "force_redeem_price": "强赎触发价",
        },
        inplace=True,
    )

    temp_df = temp_df[
        [
            "代码",
            "名称",
            "现价",
            "正股代码",
            "正股名称",
            "规模",
            "剩余规模",
            "转股起始日",
            "最后交易日",
            "到期日",
            "转股价",
            "强赎触发比",
            "强赎触发价",
            "正股价",
            "强赎价",
            "强赎天计数",
            "强赎条款",
            "强赎状态",
        ]
    ]
    temp_df["现价"] = pd.to_numeric(temp_df["现价"], errors="coerce")
    temp_df["规模"] = pd.to_numeric(temp_df["规模"], errors="coerce")
    temp_df["剩余规模"] = pd.to_numeric(temp_df["剩余规模"], errors="coerce")
    temp_df["转股起始日"] = pd.to_datetime(
        temp_df["转股起始日"], errors="coerce"
    ).dt.date
    temp_df["最后交易日"] = pd.to_datetime(
        temp_df["最后交易日"], errors="coerce"
    ).dt.date
    temp_df["到期日"] = pd.to_datetime(temp_df["到期日"], errors="coerce").dt.date
    temp_df["转股价"] = pd.to_numeric(temp_df["转股价"], errors="coerce")
    temp_df["强赎触发比"] = pd.to_numeric(
        temp_df["强赎触发比"].str.strip("%"), errors="coerce"
    )
    temp_df["强赎触发价"] = pd.to_numeric(temp_df["强赎触发价"], errors="coerce")
    temp_df["正股价"] = pd.to_numeric(temp_df["正股价"], errors="coerce")
    temp_df["强赎价"] = pd.to_numeric(temp_df["强赎价"], errors="coerce")
    temp_df["强赎天计数"] = temp_df["强赎天计数"].replace(
        r"^.*?(\d{1,2}\/\d{1,2} \| \d{1,2}).*?$", r"\1", regex=True
    )
    temp_df["强赎状态"] = temp_df["强赎状态"].map(
        {
            "R": "已公告强赎",
            "O": "公告要强赎",
            "G": "公告不强赎",
            "B": "已满足强赎条件",
            "": "",
        }
    )
    return temp_df


def bond_cb_adj_logs_jsl(symbol: str = "128013") -> pd.DataFrame:
    """
    集思录-可转债转股价-调整记录
    https://www.jisilu.cn/data/cbnew/#cb
    :param symbol: 可转债代码
    :type symbol: str
    :return: 转股价调整记录
    :rtype: pandas.DataFrame
    """
    url = f"https://www.jisilu.cn/data/cbnew/adj_logs/?bond_id={symbol}"
    r = requests.get(url)
    data_text = r.text
    if "</table>" not in data_text:
        # 1. 该可转债没有转股价调整记录，服务端返回文本 '暂无数据'
        # 2. 无效可转债代码，服务端返回 {"timestamp":1639565628,"isError":1,"msg":"无效代码格式"}
        # 以上两种情况，返回空的 DataFrame
        return pd.DataFrame()
    else:
        temp_df = pd.read_html(StringIO(data_text), parse_dates=True)[0]
        temp_df.columns = [item.replace(" ", "") for item in temp_df.columns]
        temp_df["下修前转股价"] = pd.to_numeric(
            temp_df["下修前转股价"], errors="coerce"
        )
        temp_df["下修后转股价"] = pd.to_numeric(
            temp_df["下修后转股价"], errors="coerce"
        )
        temp_df["下修底价"] = pd.to_numeric(temp_df["下修底价"], errors="coerce")
        temp_df["股东大会日"] = pd.to_datetime(
            temp_df["股东大会日"], format="%Y-%m-%d", errors="coerce"
        ).dt.date
        temp_df["新转股价生效日期"] = pd.to_datetime(
            temp_df["新转股价生效日期"], format="%Y-%m-%d", errors="coerce"
        ).dt.date
        return temp_df


if __name__ == "__main__":
    # 测试1：集思录可转债等权指数
    print("=" * 60)
    print("测试1：集思录可转债等权指数")
    print("=" * 60)
    bond_cb_index_jsl_df = bond_cb_index_jsl()
    print(bond_cb_index_jsl_df)

    # 测试2：使用 Playwright 登录获取可转债数据（推荐方式）
    print("\n" + "=" * 60)
    print("测试2：使用 Playwright 登录获取集思录可转债数据")
    print("=" * 60)
    # 使用用户名密码登录
    bond_cb_jsl_df = bond_cb_jsl(
        user=os.environ.get("JSL_USER", ""),
        password=os.environ.get("JSL_PASSWORD", "")
    )
    print(f"获取到 {len(bond_cb_jsl_df)} 条数据")
    print(bond_cb_jsl_df.head())
    
    # 验证数据完整性
    if len(bond_cb_jsl_df) > 30:
        print(f"\n✓ Playwright 登录测试通过！获取 {len(bond_cb_jsl_df)} 条数据")
    else:
        print(f"\n✗ 测试失败，只获取 {len(bond_cb_jsl_df)} 条数据")

    # 测试3：集思录可转债强赎
    print("\n" + "=" * 60)
    print("测试3：集思录可转债强赎")
    print("=" * 60)
    bond_cb_redeem_jsl_df = bond_cb_redeem_jsl()
    print(bond_cb_redeem_jsl_df)

    # 测试4：转股价调整记录
    print("\n" + "=" * 60)
    print("测试4：转股价调整记录")
    print("=" * 60)
    bond_cb_adj_logs_jsl_df = bond_cb_adj_logs_jsl(symbol="128013")
    print(bond_cb_adj_logs_jsl_df)
