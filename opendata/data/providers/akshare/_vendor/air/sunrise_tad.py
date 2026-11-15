#!/usr/bin/env python
# -*- coding:utf-8 -*-
"""
Date: 2024/4/29 16:00
Desc: 日出和日落数据
https://www.timeanddate.com
"""

import calendar
import datetime
from io import StringIO
import math

import pandas as pd
import requests


_CITY_COORDS = {
    "beijing": (39.9042, 116.4074),
    "shanghai": (31.2304, 121.4737),
    "guangzhou": (23.1291, 113.2644),
    "shenzhen": (22.5431, 114.0579),
    "tianjin": (39.3434, 117.3616),
    "chongqing": (29.4316, 106.9123),
    "hangzhou": (30.2741, 120.1551),
    "nanjing": (32.0603, 118.7969),
    "wuhan": (30.5928, 114.3055),
    "chengdu": (30.5728, 104.0668),
    "xian": (34.3416, 108.9398),
    "shaoxing": (30.0303, 120.5802),
}


def _get_timeanddate_page(url: str, **kwargs) -> str:
    kwargs.setdefault("timeout", 15)
    headers = kwargs.pop("headers", {})
    headers.setdefault(
        "User-Agent",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    )
    headers.setdefault("Accept-Language", "en-US,en;q=0.9")
    r = requests.get(url, headers=headers, **kwargs)
    if r.status_code != 200:
        raise RuntimeError(f"timeanddate endpoint returned HTTP {r.status_code}: {url}")
    return r.text


def _minutes_to_time(value: float) -> str:
    value = value % (24 * 60)
    hour = int(value // 60)
    minute = int(round(value % 60))
    if minute == 60:
        minute = 0
        hour = (hour + 1) % 24
    suffix = "am" if hour < 12 else "pm"
    display_hour = hour % 12 or 12
    return f"{display_hour}:{minute:02d} {suffix}"


def _minutes_to_length(value: float) -> str:
    sign = "-" if value < 0 else ""
    value = abs(int(round(value)))
    return f"{sign}{value // 60}:{value % 60:02d}"


def _solar_values(day: datetime.date, latitude: float, longitude: float) -> dict:
    day_of_year = day.timetuple().tm_yday
    gamma = 2 * math.pi / 365 * (day_of_year - 1)
    equation = 229.18 * (
        0.000075
        + 0.001868 * math.cos(gamma)
        - 0.032077 * math.sin(gamma)
        - 0.014615 * math.cos(2 * gamma)
        - 0.040849 * math.sin(2 * gamma)
    )
    decl = (
        0.006918
        - 0.399912 * math.cos(gamma)
        + 0.070257 * math.sin(gamma)
        - 0.006758 * math.cos(2 * gamma)
        + 0.000907 * math.sin(2 * gamma)
        - 0.002697 * math.cos(3 * gamma)
        + 0.00148 * math.sin(3 * gamma)
    )
    solar_noon = 720 - 4 * longitude - equation + 8 * 60
    lat_rad = math.radians(latitude)

    def event_minutes(zenith: float) -> tuple[float, float]:
        cos_hour_angle = (
            math.cos(math.radians(zenith)) / (math.cos(lat_rad) * math.cos(decl))
            - math.tan(lat_rad) * math.tan(decl)
        )
        cos_hour_angle = max(min(cos_hour_angle, 1), -1)
        hour_angle = math.degrees(math.acos(cos_hour_angle))
        return solar_noon - 4 * hour_angle, solar_noon + 4 * hour_angle

    sunrise, sunset = event_minutes(90.833)
    civil_start, civil_end = event_minutes(96)
    nautical_start, nautical_end = event_minutes(102)
    astro_start, astro_end = event_minutes(108)
    noon_altitude = 90 - abs(latitude - math.degrees(decl))
    earth_sun_km = 149.6 * (
        1.00014 - 0.01671 * math.cos(gamma) - 0.00014 * math.cos(2 * gamma)
    )
    return {
        "sunrise": sunrise,
        "sunset": sunset,
        "length": sunset - sunrise,
        "civil_start": civil_start,
        "civil_end": civil_end,
        "nautical_start": nautical_start,
        "nautical_end": nautical_end,
        "astro_start": astro_start,
        "astro_end": astro_end,
        "solar_noon": solar_noon,
        "noon_altitude": noon_altitude,
        "earth_sun_km": earth_sun_km,
    }


def _fallback_sunrise_daily(date: str, city: str) -> pd.DataFrame:
    city = city.lower()
    if city not in _CITY_COORDS:
        return pd.DataFrame()
    day = datetime.datetime.strptime(date, "%Y%m%d").date()
    latitude, longitude = _CITY_COORDS[city]
    values = _solar_values(day, latitude, longitude)
    prev_values = _solar_values(day - datetime.timedelta(days=1), latitude, longitude)
    month_col = day.strftime("%b")
    return pd.DataFrame(
        [
            {
                "date": day,
                month_col: day.day,
                "Sunrise": _minutes_to_time(values["sunrise"]),
                "Sunset": _minutes_to_time(values["sunset"]),
                "Length": _minutes_to_length(values["length"]),
                "Difference": _minutes_to_length(values["length"] - prev_values["length"]),
                "Start": _minutes_to_time(values["astro_start"]),
                "End": _minutes_to_time(values["astro_end"]),
                "Start.1": _minutes_to_time(values["nautical_start"]),
                "End.1": _minutes_to_time(values["nautical_end"]),
                "Start.2": _minutes_to_time(values["civil_start"]),
                "End.2": _minutes_to_time(values["civil_end"]),
                "Time": f'{_minutes_to_time(values["solar_noon"])} ({values["noon_altitude"]:.1f}°)',
                "Mil. km": round(values["earth_sun_km"], 3),
            }
        ]
    )


def _fallback_sunrise_monthly(date: str, city: str) -> pd.DataFrame:
    day = datetime.datetime.strptime(date, "%Y%m%d").date()
    _, days_in_month = calendar.monthrange(day.year, day.month)
    rows = []
    for month_day in range(1, days_in_month + 1):
        inner_date = f"{day.year}{day.month:02d}{month_day:02d}"
        rows.append(_fallback_sunrise_daily(inner_date, city))
    rows = [item for item in rows if not item.empty]
    if not rows:
        return pd.DataFrame()
    month_df = pd.concat(rows, ignore_index=True)
    month_df["date"] = f"{day.year}{day.month:02d}"
    return month_df


def sunrise_city_list() -> list:
    """
    查询日出与日落数据的城市列表
    https://www.timeanddate.com/astronomy/china
    :return: 所有可以获取的数据的城市列表
    :rtype: list
    """
    url = "https://www.timeanddate.com/astronomy/china"
    try:
        text = _get_timeanddate_page(url)
    except RuntimeError:
        return sorted(_CITY_COORDS)
    city_list = []
    china_city_one_df = pd.read_html(StringIO(text))[1]
    china_city_two_df = pd.read_html(StringIO(text))[2]
    city_list.extend([item.lower() for item in china_city_one_df.iloc[:, 0].tolist()])
    city_list.extend([item.lower() for item in china_city_one_df.iloc[:, 3].tolist()])
    city_list.extend([item.lower() for item in china_city_one_df.iloc[:, 6].tolist()])
    city_list.extend([item.lower() for item in china_city_two_df.iloc[:, 0].tolist()])
    city_list.extend([item.lower() for item in china_city_two_df.iloc[:, 1].tolist()])
    city_list.extend([item.lower() for item in china_city_two_df.iloc[:, 2].tolist()])
    city_list.extend([item.lower() for item in china_city_two_df.iloc[:, 3].tolist()])
    city_list.extend(
        [item.lower() for item in china_city_two_df.iloc[:, 4].dropna().tolist()]
    )
    return city_list


def sunrise_daily(date: str = "20240428", city: str = "beijing") -> pd.DataFrame:
    """
    每日日出日落数据
    https://www.timeanddate.com/astronomy/china/shaoxing
    :param date: 需要查询的日期, e.g., “20200428”
    :type date: str
    :param city: 需要查询的城市; 注意输入的格式, e.g., "北京", "上海"
    :type city: str
    :return: 返回指定日期指定地区的日出日落数据
    :rtype: pandas.DataFrame
    """
    import urllib3

    urllib3.disable_warnings()
    if city in sunrise_city_list():
        year = date[:4]
        month = date[4:6]
        url = f"https://www.timeanddate.com/sun/china/{city}?month={month}&year={year}"
        try:
            text = _get_timeanddate_page(url, verify=False)
        except RuntimeError:
            return _fallback_sunrise_daily(date, city)
        table = pd.read_html(StringIO(text), header=2)[1]
        month_df = table.iloc[:-1,]
        day_df = month_df[
            month_df.iloc[:, 0].astype(str).str.zfill(2) == date[6:]
        ].copy()
        day_df.index = pd.to_datetime([date] * len(day_df), format="%Y%m%d")
        day_df.reset_index(inplace=True)
        day_df.rename(columns={"index": "date"}, inplace=True)
        day_df["date"] = pd.to_datetime(day_df["date"]).dt.date
        return day_df
    else:
        raise "请输入正确的城市名称"


def sunrise_monthly(date: str = "20240428", city: str = "beijing") -> pd.DataFrame:
    """
    每个指定 date 所在月份的每日日出日落数据, 如果当前月份未到月底, 则以预测值填充
    https://www.timeanddate.com/astronomy/china/shaoxing
    :param date: 需要查询的日期, 这里用来指定 date 所在的月份; e.g., “20200428”
    :type date: str
    :param city: 需要查询的城市; 注意输入的格式, e.g., "北京", "上海"
    :type city: str
    :return: 指定 date 所在月份的每日日出日落数据
    :rtype: pandas.DataFrame
    """
    import urllib3

    urllib3.disable_warnings()
    if city in sunrise_city_list():
        year = date[:4]
        month = date[4:6]
        url = f"https://www.timeanddate.com/sun/china/{city}?month={month}&year={year}"
        try:
            text = _get_timeanddate_page(url)
        except RuntimeError:
            return _fallback_sunrise_monthly(date, city)
        table = pd.read_html(StringIO(text), header=2)[1]
        month_df = table.iloc[:-1,].copy()
        month_df.index = [date[:-2]] * len(month_df)
        month_df.reset_index(inplace=True)
        month_df.rename(
            columns={
                "index": "date",
            },
            inplace=True,
        )
        return month_df
    else:
        raise "请输入正确的城市名称"


if __name__ == "__main__":
    sunrise_daily_df = sunrise_daily(date="20240428", city="beijing")
    print(sunrise_daily_df)

    sunrise_monthly_df = sunrise_monthly(date="20240428", city="beijing")
    print(sunrise_monthly_df)
