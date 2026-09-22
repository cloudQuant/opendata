"""
Stock Zh A Hist Em

数据源: AkShare
函数: stock_zh_a_hist
频率: daily
"""

from typing import Any

import pandas as pd

from opendata.data_fetch.providers.akshare_provider import AkshareProvider


class StockZhAHistEm(AkshareProvider):
    """A股历史行情数据"""

    def __init__(
        self,
        db_url: str | None = None,
        logger: Any = None,  # noqa: ANN401
    ) -> None:
        super().__init__(db_url, logger)
        self.table_name = "stock_zh_a_hist"
        self.create_table_sql = """
        CREATE TABLE IF NOT EXISTS `stock_zh_a_hist` (
            `R_ID` INT AUTO_INCREMENT PRIMARY KEY,
            `股票代码` VARCHAR(20) COMMENT '股票代码',
            `日期` DATE COMMENT '交易日期',
            `开盘` DECIMAL(10, 2) COMMENT '开盘价',
            `收盘` DECIMAL(10, 2) COMMENT '收盘价',
            `最高` DECIMAL(10, 2) COMMENT '最高价',
            `最低` DECIMAL(10, 2) COMMENT '最低价',
            `成交量` BIGINT COMMENT '成交量',
            `成交额` BIGINT COMMENT '成交额',
            `振幅` DECIMAL(10, 2) COMMENT '振幅',
            `涨跌幅` DECIMAL(10, 2) COMMENT '涨跌幅',
            `涨跌额` DECIMAL(10, 2) COMMENT '涨跌额',
            `换手率` DECIMAL(10, 2) COMMENT '换手率',
            `created_at` DATETIME DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
            `updated_at` DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
            UNIQUE KEY uk_code_date (`股票代码`, `日期`),
            INDEX idx_date (`日期`),
            INDEX idx_code (`股票代码`)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='A股历史行情数据'
        """

    def fetch_data(
        self,
        symbol: str = "000001",
        period: str = "daily",
        start_date: str = "20240101",
        adjust: str = "qfq",
    ) -> pd.DataFrame:
        """
        获取A股历史行情数据

        Args:
            symbol: 股票代码
            period: 数据周期 daily/weekly/monthly
            start_date: 开始日期
            adjust: 复权类型 qfq/hfq/"" (前复权/后复权/不复权)

        Returns:
            pd.DataFrame: 获取的数据
        """
        try:
            # 从AkShare获取数据
            df = self.fetch_ak_data(
                "stock_zh_a_hist",
                symbol=symbol,
                period=period,
                start_date=start_date,
                adjust=adjust,
            )

            if df is None or df.empty:
                self.logger.warning(f"股票{symbol}没有获取到数据")
                return pd.DataFrame()

            # 创建表
            self.create_table_if_not_exists(self.table_name, self.create_table_sql)

            # 保存到数据库
            self.save_data(df, self.table_name, ignore_duplicates=True)

            self.logger.info(f"成功获取股票{symbol}历史数据，共{len(df)}行")
            return df

        except Exception as e:
            self.logger.error(f"获取股票{symbol}历史数据失败: {e}")
            return pd.DataFrame()


def main() -> None:
    """主函数"""
    script = StockZhAHistEm()
    script.fetch_data()


if __name__ == "__main__":
    main()
