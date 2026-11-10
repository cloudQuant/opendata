"""Binance kline provider writing into the ``akshare_data`` warehouse.

Mirrors the ``AkshareToMySql`` contract (no-arg constructible, ``fetch_data``
entrypoint, ``table_name`` + ``create_table_sql``) so the existing script
scanner and scheduled-task runner pick these scripts up unchanged.
"""

from __future__ import annotations

import hashlib
import logging
from datetime import date, datetime, timedelta, timezone
from typing import Any

import pandas as pd

from opendata.data_fetch.configs.db_config import DB_CONFIG
from opendata.data_fetch.core.mysql_base import MysqlBase
from opendata.data_fetch.providers.binance_vision import BinanceClient, date_to_ms

BINANCE_MARKET = "BINANCE"

#: Core spot universe.  Delisted/renamed bases are excluded: MATIC migrated to
#: POL, FTT no longer trades, FTM became S.  A symbol that is not on Binance
#: simply reports as a per-symbol failure during backfill.
DEFAULT_CRYPTO_SYMBOLS = (
    "BTCUSDT",
    "ETHUSDT",
    "BNBUSDT",
    "SOLUSDT",
    "XRPUSDT",
    "ADAUSDT",
    "DOGEUSDT",
    "AVAXUSDT",
    "TRXUSDT",
    "LINKUSDT",
    "LTCUSDT",
    "BCHUSDT",
    "ATOMUSDT",
    "DOTUSDT",
    "UNIUSDT",
    "NEARUSDT",
    "APTUSDT",
    "ARBUSDT",
    "OPUSDT",
    "FILUSDT",
    "ETCUSDT",
    "XLMUSDT",
    "ALGOUSDT",
    "HBARUSDT",
    "INJUSDT",
    "SUIUSDT",
    "SEIUSDT",
    "TIAUSDT",
    "POLUSDT",
    "AAVEUSDT",
)

BAR_COLUMNS = (
    "MARKET",
    "SYMBOL",
    "OPEN_PRICE",
    "HIGH_PRICE",
    "LOW_PRICE",
    "CLOSE_PRICE",
    "VOLUME",
    "TURNOVER",
    "TRADE_COUNT",
    "TAKER_BUY_VOLUME",
    "TAKER_BUY_TURNOVER",
    "OPEN_TIME_MS",
    "CLOSE_TIME_MS",
)


class BinanceToMySql(MysqlBase):
    """Stores Binance spot klines in one warehouse table per frequency."""

    #: supplied by the concrete per-frequency subclass (e.g. CryptoDailyMarket)
    table_name: str
    create_table_sql: str

    #: column holding the bar timestamp -- DATE for daily, DATETIME for hourly
    time_column: str = "TRADE_DATE"
    #: the Binance interval this provider writes
    interval: str = "1d"
    #: extra reference columns matching the warehouse convention
    reference_code: str = "CRYPTO_DAILY"

    def __init__(
        self, db_config: dict[str, Any] | None = None, logger: logging.Logger | None = None
    ):
        super().__init__(db_config or DB_CONFIG, logger or logging.getLogger("BinanceToMySql"))
        self.client = BinanceClient(logger=self.logger)

    @property
    def unique_keys(self) -> list[str]:
        return ["MARKET", "SYMBOL", self.time_column]

    # -- mapping ------------------------------------------------------------

    def _bar_timestamp(self, bar: dict[str, Any]) -> date | datetime:
        trade_time = bar["trade_time"]
        if self.time_column == "TRADE_DATE":
            return trade_time.date()
        return trade_time.replace(minute=0, second=0, microsecond=0)

    def bars_to_frame(self, bars: list[dict[str, Any]], symbol: str) -> pd.DataFrame:
        if not bars:
            return pd.DataFrame(columns=["R_ID", *BAR_COLUMNS, self.time_column])
        rows = []
        for bar in bars:
            stamp = self._bar_timestamp(bar)
            rows.append(
                {
                    # Deterministic so a re-run keeps row identity instead of
                    # churning the primary key on every upsert.
                    "R_ID": hashlib.md5(
                        f"{BINANCE_MARKET}|{symbol.upper()}|{stamp}|{self.interval}".encode(),
                        usedforsecurity=False,
                    ).hexdigest(),
                    "MARKET": BINANCE_MARKET,
                    "SYMBOL": symbol.upper(),
                    self.time_column: stamp,
                    "OPEN_PRICE": bar["open"],
                    "HIGH_PRICE": bar["high"],
                    "LOW_PRICE": bar["low"],
                    "CLOSE_PRICE": bar["close"],
                    "VOLUME": bar["volume"],
                    "TURNOVER": bar["quote_volume"],
                    "TRADE_COUNT": bar["trade_count"],
                    "TAKER_BUY_VOLUME": bar["taker_buy_volume"],
                    "TAKER_BUY_TURNOVER": bar["taker_buy_quote_volume"],
                    "OPEN_TIME_MS": bar["open_time"],
                    "CLOSE_TIME_MS": bar["close_time"],
                }
            )
        return pd.DataFrame(rows)

    def ensure_table(self) -> None:
        self.connect_db()
        self.require_cursor().execute(self.create_table_sql)
        self.require_connection().commit()

    def store(self, bars: list[dict[str, Any]], symbol: str) -> int | bool:
        """Idempotent upsert keyed on the table's unique index."""
        frame = self.bars_to_frame(bars, symbol)
        if frame.empty:
            return False
        self.ensure_table()
        return self.save_data(
            frame,
            self.table_name,
            on_duplicate_update=True,
            unique_keys=self.unique_keys,
        )

    # -- incremental read path used by the scheduled scripts ----------------

    def backfill_history(
        self,
        symbol: str,
        *,
        start: date,
        end: date | None = None,
        source: str = "rest",
    ) -> int:
        """Load historical bars.

        ``rest`` pages 1000 bars per request (BTC daily history is 4 calls),
        ``archive`` walks the monthly/daily CSV zips which cost no request
        weight but need one HTTP call per month.
        """
        until = end or datetime.now(tz=timezone.utc).date()
        if source == "archive":
            bars = list(
                self.client.iter_archive_bars(symbol, self.interval, start=start, end=until)
            )
        else:
            bars = list(
                self.client.iter_klines(
                    symbol,
                    self.interval,
                    since_ms=date_to_ms(start),
                    until_ms=date_to_ms(until, end_of_day=True),
                )
            )
        return int(self.store(bars, symbol) or 0)

    def refresh_tail(self, symbol: str, *, days: int = 3) -> int:
        """REST load for the most recent bars, including the live open bar."""
        since = date_to_ms(datetime.now(tz=timezone.utc).date() - timedelta(days=days))
        bars = list(self.client.iter_klines(symbol, self.interval, since_ms=since))
        if bars:
            self.logger.info(
                "tail %s %s -> %s UTC", symbol, bars[0]["trade_time"], bars[-1]["trade_time"]
            )
        return int(self.store(bars, symbol) or 0)


def daily_table_ddl(table_name: str) -> str:
    return f"""
        CREATE TABLE IF NOT EXISTS `{table_name}` (
            `R_ID` VARCHAR(64) NOT NULL COMMENT '主键ID',
            `REFERENCE_CODE` VARCHAR(50) DEFAULT 'CRYPTO_DAILY' COMMENT '参考编码',
            `REFERENCE_NAME` VARCHAR(100) DEFAULT '数字货币历史行情' COMMENT '参考名称',
            `SYMBOL` VARCHAR(20) NOT NULL COMMENT '交易对(Binance 风格, 如 BTCUSDT)',
            `MARKET` VARCHAR(20) NOT NULL COMMENT '交易所(BINANCE)',
            `TRADE_DATE` DATE NOT NULL COMMENT '交易日(UTC)',
            `OPEN_PRICE` DECIMAL(28, 12) COMMENT '开盘价',
            `HIGH_PRICE` DECIMAL(28, 12) COMMENT '最高价',
            `LOW_PRICE` DECIMAL(28, 12) COMMENT '最低价',
            `CLOSE_PRICE` DECIMAL(28, 12) COMMENT '收盘价',
            `VOLUME` DECIMAL(30, 8) COMMENT '成交量(基础币)',
            `TURNOVER` DECIMAL(30, 8) COMMENT '成交额(计价币)',
            `TRADE_COUNT` BIGINT COMMENT '成交笔数',
            `TAKER_BUY_VOLUME` DECIMAL(30, 8) COMMENT '主动买入量',
            `TAKER_BUY_TURNOVER` DECIMAL(30, 8) COMMENT '主动买入额',
            `OPEN_TIME_MS` BIGINT COMMENT 'K线开盘时间戳(ms, UTC)',
            `CLOSE_TIME_MS` BIGINT COMMENT 'K线收盘时间戳(ms, UTC)',
            `IS_ACTIVE` TINYINT(1) DEFAULT 1 COMMENT '是否有效',
            `DATA_SOURCE` VARCHAR(50) DEFAULT 'binance_vision' COMMENT '数据来源',
            `CREATEDATE` DATETIME DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
            `CREATEUSER` VARCHAR(50) DEFAULT 'system' COMMENT '创建人',
            `UPDATEDATE` DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
            `UPDATEUSER` VARCHAR(50) DEFAULT 'system' COMMENT '更新人',
            PRIMARY KEY (`R_ID`),
            UNIQUE KEY `UK_{table_name}_SYMBOL_DATE` (`MARKET`, `SYMBOL`, `TRADE_DATE`),
            KEY `IDX_{table_name}_SYMBOL` (`MARKET`, `SYMBOL`),
            KEY `IDX_{table_name}_DATE` (`TRADE_DATE`)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='数字货币 Binance 日线历史行情'
    """


def hourly_table_ddl(table_name: str) -> str:
    return f"""
        CREATE TABLE IF NOT EXISTS `{table_name}` (
            `R_ID` VARCHAR(64) NOT NULL COMMENT '主键ID',
            `REFERENCE_CODE` VARCHAR(50) DEFAULT 'CRYPTO_HOURLY' COMMENT '参考编码',
            `REFERENCE_NAME` VARCHAR(100) DEFAULT '数字货币小时行情' COMMENT '参考名称',
            `SYMBOL` VARCHAR(20) NOT NULL COMMENT '交易对(Binance 风格, 如 BTCUSDT)',
            `MARKET` VARCHAR(20) NOT NULL COMMENT '交易所(BINANCE)',
            `TRADE_DATETIME` DATETIME NOT NULL COMMENT 'K线开盘时间(UTC)',
            `TRADE_DATE` DATE GENERATED ALWAYS AS (DATE(`TRADE_DATETIME`)) STORED COMMENT '交易日',
            `OPEN_PRICE` DECIMAL(28, 12) COMMENT '开盘价',
            `HIGH_PRICE` DECIMAL(28, 12) COMMENT '最高价',
            `LOW_PRICE` DECIMAL(28, 12) COMMENT '最低价',
            `CLOSE_PRICE` DECIMAL(28, 12) COMMENT '收盘价',
            `VOLUME` DECIMAL(30, 8) COMMENT '成交量(基础币)',
            `TURNOVER` DECIMAL(30, 8) COMMENT '成交额(计价币)',
            `TRADE_COUNT` BIGINT COMMENT '成交笔数',
            `TAKER_BUY_VOLUME` DECIMAL(30, 8) COMMENT '主动买入量',
            `TAKER_BUY_TURNOVER` DECIMAL(30, 8) COMMENT '主动买入额',
            `OPEN_TIME_MS` BIGINT COMMENT 'K线开盘时间戳(ms, UTC)',
            `CLOSE_TIME_MS` BIGINT COMMENT 'K线收盘时间戳(ms, UTC)',
            `IS_ACTIVE` TINYINT(1) DEFAULT 1 COMMENT '是否有效',
            `DATA_SOURCE` VARCHAR(50) DEFAULT 'binance_vision' COMMENT '数据来源',
            `CREATEDATE` DATETIME DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
            `CREATEUSER` VARCHAR(50) DEFAULT 'system' COMMENT '创建人',
            `UPDATEDATE` DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
            `UPDATEUSER` VARCHAR(50) DEFAULT 'system' COMMENT '更新人',
            PRIMARY KEY (`R_ID`),
            UNIQUE KEY `UK_{table_name}_SYMBOL_BAR` (`MARKET`, `SYMBOL`, `TRADE_DATETIME`),
            KEY `IDX_{table_name}_SYMBOL` (`MARKET`, `SYMBOL`),
            KEY `IDX_{table_name}_DATETIME` (`TRADE_DATETIME`),
            KEY `IDX_{table_name}_DATE` (`TRADE_DATE`)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='数字货币 Binance 小时线历史行情'
    """
