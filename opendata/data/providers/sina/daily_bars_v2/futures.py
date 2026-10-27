"""Offline normalizer for the recorded Sina commodity-futures daily shape."""

from opendata.data.providers.sina.daily_bars_v2._base import SinaDailyBarV2Normalizer
from opendata.data.providers.sina.daily_bars_v2.contract import FUTURES_DAILY_SINA_PROFILE


class SinaFuturesDailyV2Adapter(SinaDailyBarV2Normalizer):
    """Normalize an already-collected ``futures_zh_daily_sina`` frame.

    This adapter does not fetch data. Its source-native volume and price units
    remain unverified, and its reviewed source profile declares no amount
    column. The output is an OHLCV projection; ``hold`` and ``settle`` are
    deliberately omitted and listed on each row. Retain the raw source frame
    separately if those fields or full-fidelity reconstruction are required.
    """

    profile = FUTURES_DAILY_SINA_PROFILE
