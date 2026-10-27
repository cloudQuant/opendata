"""Offline normalizer for the recorded Sina SSE option daily shape."""

from opendata.data.providers.sina.daily_bars_v2._base import SinaDailyBarV2Normalizer
from opendata.data.providers.sina.daily_bars_v2.contract import OPTION_DAILY_SINA_PROFILE


class SinaOptionDailyV2Adapter(SinaDailyBarV2Normalizer):
    """Normalize an already-collected ``option_sse_daily_sina`` frame.

    This adapter does not fetch data. Its source-native volume and price units
    remain unverified, and its reviewed source profile declares no amount
    column. The output is an OHLCV projection, not the complete source frame;
    retain the raw frame separately for full-fidelity source preservation.
    """

    profile = OPTION_DAILY_SINA_PROFILE
