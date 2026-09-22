"""
Data scripts for akshare data acquisition

Scripts are organized by:
- Category: stocks, funds, futures, indexs, options, bonds, currencies, forexs, cryptos, common
- Frequency: daily, weekly, monthly, hourly

Each script should:
1. Inherit from AkshareProvider or provide a fetch_data() method
2. Define table_name and optionally create_table_sql
3. Be discoverable via the script scanner
"""
