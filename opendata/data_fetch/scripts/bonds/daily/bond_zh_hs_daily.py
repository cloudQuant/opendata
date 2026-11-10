"""
Bond Zh Hs Daily

数据源: AkShare
函数: bond_zh_hs_daily
频率: daily
"""

import pandas as pd

from opendata.data_fetch.configs.db_config import DB_CONFIG
from opendata.data_fetch.providers.akshare_to_mysql import AkshareToMySql


class BondZhHsDaily(AkshareToMySql):
    """Bond Zh Hs Daily"""

    def __init__(self, db_config=DB_CONFIG, logger=None):
        super().__init__(db_config, logger)
        self.table_name = "BOND_ZH_HS_DAILY"
        self.create_table_sql = """
    CREATE TABLE IF NOT EXISTS `BOND_ZH_HS_DAILY` (
        `R_ID` INT AUTO_INCREMENT PRIMARY KEY,
            `symbol` VARCHAR(50) COMMENT '品种代码',
            `name` VARCHAR(100) COMMENT '品种名称',
            `data_date` DATE COMMENT '数据日期',
            `created_at` DATETIME DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
            `updated_at` DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
        UNIQUE KEY uk_symbol_date (`symbol`, `data_date`),
        INDEX idx_data_date (`data_date`)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='Bond Zh Hs Daily'
    """

    def fetch_data(self, **kwargs):
        """Fetch data from AkShare and save to database.

        Args:
            **kwargs: Parameters to pass to ak.bond_zh_hs_daily

        Returns:
            pd.DataFrame: Fetched data
        """
        try:
            # Fetch data from AkShare
            df = self.fetch_ak_data("bond_zh_hs_daily", **kwargs)

            if df is None or df.empty:
                self.logger.warning("No data found")
                return pd.DataFrame()

            # Process data if needed
            # AkShare returns the bar columns only; the requested symbol is the
            # instrument identity and `date` is the trading session, so the
            # unique key (symbol, data_date) would otherwise dedup on run date.
            if "date" in df.columns:
                df["date"] = pd.to_datetime(df["date"], errors="coerce")
                df = df.dropna(subset=["date"]).drop_duplicates(subset=["date"])
                df["data_date"] = df["date"].dt.date
            elif "data_date" not in df.columns:
                df["data_date"] = pd.Timestamp.now().date()
            requested_symbol = str(kwargs.get("symbol") or "").strip()
            if not requested_symbol:
                self.logger.warning("No symbol supplied; daily bars would have no instrument key")
                return pd.DataFrame()
            if "symbol" not in df.columns:
                df["symbol"] = requested_symbol
            if "name" not in df.columns:
                df["name"] = requested_symbol

            # Save to database
            self.create_table_if_not_exists(self.table_name, self.create_table_sql)
            self.save_data(df, self.table_name, ignore_duplicates=True)

            return df

        except Exception as e:
            self.logger.error(f"Error fetching data: {e}")
            return pd.DataFrame()


def main():
    """Main function to run the data fetch"""

    script = BondZhHsDaily()
    script.fetch_data()


if __name__ == "__main__":
    main()
