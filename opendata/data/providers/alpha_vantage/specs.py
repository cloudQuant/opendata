"""Documented Alpha Vantage functions used by this provider slice."""

ALPHA_VANTAGE_SOURCE = "alpha_vantage"
ALPHA_VANTAGE_ENDPOINT = "https://www.alphavantage.co/query"
ALPHA_VANTAGE_DOCUMENTATION = "https://www.alphavantage.co/documentation/"

PRICE_INTERVALS = ("1m", "5m", "15m", "30m", "60m", "1d", "1W", "1M")
PRICE_ADJUSTMENTS = ("unadjusted", "splits_and_dividends")
INTRADAY_FIRST_MONTH = "2000-01"

FUNCTIONS = {
    "intraday": "TIME_SERIES_INTRADAY",
    "daily": "TIME_SERIES_DAILY",
    "daily_adjusted": "TIME_SERIES_DAILY_ADJUSTED",
    "weekly": "TIME_SERIES_WEEKLY",
    "weekly_adjusted": "TIME_SERIES_WEEKLY_ADJUSTED",
    "monthly": "TIME_SERIES_MONTHLY",
    "monthly_adjusted": "TIME_SERIES_MONTHLY_ADJUSTED",
    "earnings": "EARNINGS",
}
