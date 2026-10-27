"""Explicit, offline source-native daily-bar v2 adapters for Sina frames."""

from opendata.data.providers.sina.daily_bars_v2.bond import SinaBondDailyV2Adapter
from opendata.data.providers.sina.daily_bars_v2.contract import SourceNativeBarV2
from opendata.data.providers.sina.daily_bars_v2.futures import SinaFuturesDailyV2Adapter
from opendata.data.providers.sina.daily_bars_v2.option import SinaOptionDailyV2Adapter

__all__ = [
    "SinaBondDailyV2Adapter",
    "SinaFuturesDailyV2Adapter",
    "SinaOptionDailyV2Adapter",
    "SourceNativeBarV2",
]
