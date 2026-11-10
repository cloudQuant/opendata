"""Crypto Daily Market

数据源: Binance (data-api.binance.vision / data.binance.vision)
接口: /api/v3/klines (interval=1d) + spot monthly/daily kline archives
频率: daily

写入表: CRYPTO_DAILY_MARKET，唯一键 (MARKET, SYMBOL, TRADE_DATE)
"""

from __future__ import annotations

from datetime import date
from typing import Any

import pandas as pd

from opendata.data_fetch.configs.db_config import DB_CONFIG
from opendata.data_fetch.providers.binance_to_mysql import (
    DEFAULT_CRYPTO_SYMBOLS,
    BinanceToMySql,
    daily_table_ddl,
)

SCRIPT_NAME = "Crypto Daily Market"
DESCRIPTION = "数字货币 Binance 日线历史行情（现货 USDT 交易对）"
TARGET_TABLE = "CRYPTO_DAILY_MARKET"


class CryptoDailyMarket(BinanceToMySql):
    """Pull Binance daily spot klines for the configured crypto universe."""

    time_column = "TRADE_DATE"
    interval = "1d"
    reference_code = "CRYPTO_DAILY"

    def __init__(self, db_config=DB_CONFIG, logger=None):
        super().__init__(db_config, logger)
        self.table_name = TARGET_TABLE
        self.create_table_sql = daily_table_ddl(self.table_name)

    def fetch_data(self, **kwargs: Any) -> pd.DataFrame:
        """Incremental daily update: backfill history, then refresh the tail.

        Keyword args:
            symbols: iterable of Binance pair codes, defaults to the core universe
            since: first bar date to archive-load (default 2017-08-01)
            tail_days: how many recent days to re-pull over REST (default 5)
        """
        symbols = [str(s).upper() for s in kwargs.get("symbols") or DEFAULT_CRYPTO_SYMBOLS]
        since = kwargs.get("since") or date(2017, 8, 1)
        tail_days = int(kwargs.get("tail_days", 5))
        written = 0
        for symbol in symbols:
            try:
                written += self.backfill_history(symbol, start=since) or 0
                written += self.refresh_tail(symbol, days=tail_days) or 0
            except Exception as exc:
                self.logger.error("crypto daily %s failed: %s", symbol, exc)
        self.logger.info("crypto daily 写入 %s 行，覆盖 %s 个交易对", written, len(symbols))
        return pd.DataFrame({"symbol": symbols, "written": [written] * len(symbols)})


def main():
    """Main function to run the data fetch"""
    return CryptoDailyMarket().fetch_data()


if __name__ == "__main__":
    main()
