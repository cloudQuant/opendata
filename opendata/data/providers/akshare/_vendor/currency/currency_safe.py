# -*- coding:utf-8 -*-
"""Fetch the complete official SAFE RMB reference-rate series."""

from __future__ import annotations

import math
import re
from datetime import date, datetime, timedelta
from io import BytesIO, StringIO
from urllib.parse import urljoin

import pandas as pd
import requests
from bs4 import BeautifulSoup

SAFE_RATE_COLUMNS = (
    "美元",
    "欧元",
    "日元",
    "港元",
    "英镑",
    "澳元",
    "新西兰元",
    "新加坡元",
    "瑞士法郎",
    "加元",
    "澳门元",
    "林吉特",
    "卢布",
    "兰特",
    "韩元",
    "迪拉姆",
    "里亚尔",
    "福林",
    "兹罗提",
    "丹麦克朗",
    "瑞典克朗",
    "挪威克朗",
    "里拉",
    "比索",
    "泰铢",
)
DATE_COLUMN = "日期"
DEFAULT_TIMEOUT = 15.0
MAX_TIMEOUT = 60.0
_MISSING_RATE_TOKENS = {"", "-", "--", "—", "N/A", "n/a"}


def _parse_business_date(value: object) -> date:
    if pd.isna(value):
        raise ValueError("SAFE reference-rate row has an empty business date")
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip()
    for date_format in ("%Y-%m-%d", "%Y/%m/%d", "%Y%m%d", "%Y年%m月%d日"):
        try:
            return datetime.strptime(text, date_format).date()
        except ValueError:
            continue
    raise ValueError(f"SAFE returned an invalid business date: {value!r}")


def _normalize_frame(
    frame: pd.DataFrame,
    *,
    today: date,
    allow_empty: bool = False,
) -> pd.DataFrame:
    if not isinstance(frame, pd.DataFrame):
        raise TypeError("SAFE returned a non-DataFrame response")

    # Some SAFE workbook exports include a blank spreadsheet index column.
    work = frame.copy()
    work = work.loc[
        :, [not str(column).startswith("Unnamed:") for column in work.columns]
    ]
    expected_columns = [DATE_COLUMN, *SAFE_RATE_COLUMNS]
    missing = [column for column in expected_columns if column not in work.columns]
    unexpected = [column for column in work.columns if column not in expected_columns]
    if missing or unexpected:
        raise ValueError(
            f"SAFE response schema mismatch; missing={missing}, unexpected={unexpected}"
        )
    work = work.loc[:, expected_columns]
    if work.empty:
        if allow_empty:
            return work
        raise ValueError("SAFE returned no RMB reference-rate rows")

    work[DATE_COLUMN] = work[DATE_COLUMN].map(_parse_business_date)
    if (work[DATE_COLUMN] > today).any():
        future_dates = sorted(
            value.isoformat()
            for value in work.loc[work[DATE_COLUMN] > today, DATE_COLUMN].unique()
        )
        raise ValueError(f"SAFE returned future business dates: {future_dates[:5]}")

    for column in SAFE_RATE_COLUMNS:
        original = work[column]
        token = original.map(
            lambda value: str(value).strip() if not pd.isna(value) else ""
        )
        blank = original.isna() | token.isin(_MISSING_RATE_TOKENS)
        numeric = pd.to_numeric(original.mask(blank), errors="coerce")
        malformed = ~blank & numeric.isna()
        if malformed.any():
            raise ValueError(f"SAFE returned non-numeric values in {column}")
        invalid = numeric.notna() & ~numeric.map(
            lambda value: math.isfinite(float(value)) and float(value) > 0
        )
        if invalid.any():
            raise ValueError(
                f"SAFE returned non-finite or non-positive values in {column}"
            )
        work[column] = numeric

    if work.loc[:, SAFE_RATE_COLUMNS].notna().sum(axis=1).eq(0).any():
        raise ValueError("SAFE returned a business date with no valid currency rates")

    duplicates = work[work.duplicated(subset=[DATE_COLUMN], keep=False)]
    for business_date, group in duplicates.groupby(DATE_COLUMN, sort=False):
        first = group.iloc[0].loc[list(SAFE_RATE_COLUMNS)]
        if any(
            not first.equals(row.loc[list(SAFE_RATE_COLUMNS)])
            for _, row in group.iloc[1:].iterrows()
        ):
            raise ValueError(
                f"SAFE returned conflicting duplicate rows for {business_date}"
            )

    return (
        work.drop_duplicates(subset=[DATE_COLUMN], keep="first")
        .sort_values(DATE_COLUMN, kind="stable")
        .reset_index(drop=True)
    )


def currency_boc_safe(timeout: float = DEFAULT_TIMEOUT) -> pd.DataFrame:
    """Return SAFE reference rates with original units and source business dates.

    The timeout applies to every HTTP request. SAFE's downloadable workbook remains the
    historical source; the current query endpoint is requested only after its latest day.
    """
    timeout_value = float(timeout)
    if not math.isfinite(timeout_value) or not 0 < timeout_value <= MAX_TIMEOUT:
        raise ValueError(
            f"timeout must be greater than 0 and at most {MAX_TIMEOUT} seconds"
        )

    page_url = "https://www.safe.gov.cn/safe/2020/1218/17833.html"
    response = requests.get(page_url, timeout=timeout_value)
    response.raise_for_status()
    response.encoding = "utf8"
    soup = BeautifulSoup(response.text, features="lxml")
    link = soup.find(name="a", string=re.compile("人民币汇率"))
    if link is None or not link.get("href"):
        raise ValueError("SAFE page has no historical RMB reference-rate workbook link")

    workbook_url = urljoin(page_url, link["href"])
    workbook_response = requests.get(workbook_url, timeout=timeout_value)
    workbook_response.raise_for_status()
    workbook = pd.read_excel(BytesIO(workbook_response.content))
    today = datetime.now().date()
    historical = _normalize_frame(workbook, today=today)

    next_source_date = max(historical[DATE_COLUMN]) + timedelta(days=1)
    if next_source_date > today:
        return historical

    query_url = "https://www.safe.gov.cn/AppStructured/hlw/RMBQuery.do"
    query_response = requests.post(
        query_url,
        data={
            "startDate": next_source_date.isoformat(),
            "endDate": today.isoformat(),
            "queryYN": "true",
        },
        timeout=timeout_value,
    )
    query_response.raise_for_status()
    tables = pd.read_html(StringIO(query_response.text))
    if not tables:
        raise ValueError("SAFE current reference-rate response contains no table")
    current = _normalize_frame(tables[-1], today=today, allow_empty=True)
    if current.empty:
        return historical
    combined = pd.concat([historical, current], ignore_index=True)
    return _normalize_frame(combined, today=today)


if __name__ == "__main__":
    print(currency_boc_safe())
