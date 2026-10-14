"""Routing label of this provider package."""

from pathlib import Path

SOURCE = Path(__file__).resolve().parent.name

NASDAQ_COM_BASE_URL = "https://api.nasdaq.com"
NASDAQ_TRADER_DIRECTORY_URL = "https://www.nasdaqtrader.com/dynamic/SymDir/nasdaqtraded.txt"
NASDAQ_DATA_LINK_BASE_URL = "https://data.nasdaq.com"

NASDAQ_COM_HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Origin": "https://www.nasdaq.com",
    "Referer": "https://www.nasdaq.com/",
}
