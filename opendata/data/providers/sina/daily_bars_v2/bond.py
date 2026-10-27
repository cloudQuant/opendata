"""Offline normalizer for the recorded Sina convertible-bond daily shape."""

from opendata.data.providers.sina.daily_bars_v2._base import SinaDailyBarV2Normalizer
from opendata.data.providers.sina.daily_bars_v2.contract import BOND_DAILY_SINA_PROFILE


class SinaBondDailyV2Adapter(SinaDailyBarV2Normalizer):
    """Normalize an already-collected ``bond_zh_hs_cov_daily`` frame.

    This adapter does not fetch data. Its source-native volume and price units
    remain unverified, and its reviewed source profile declares no amount
    column. The output is an OHLCV projection, not the complete source frame;
    retain the raw frame separately for full-fidelity source preservation.
    """

    profile = BOND_DAILY_SINA_PROFILE

    def normalize_symbol(self, symbol: str) -> str:
        """Return the expected Sina code without guessing unknown exchanges."""
        code = symbol.strip().lower()
        if code.startswith(("sh", "sz", "bj")):
            return code
        if code.startswith("11"):
            return f"sh{code}"
        if code.startswith("12"):
            return f"sz{code}"
        return code
