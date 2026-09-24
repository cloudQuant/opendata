"""Convertible-bond daily bars fetcher (domain ``bond_daily``, contract ``Bar``).

Wraps ``opendata_http.bond_zh_hs_cov_daily`` (sina Shanghai/Shenzhen
convertible-bond daily line). B1.2 registration: ``verified=false``
keeps the capability out of ``source=auto`` routing until the AC-6 P1
fidelity sampling lands.

The source frame carries English column names and no turnover column,
so ``amount`` is 0.0 (honest absence) and ``volume`` keeps the sina
unit (手, treated as the traded-bond count for this source - no §8.2
conversion is claimed). ``symbol`` is the sina spelling (``sh010107``):
bond codes do not follow the A-share prefix inference, so the query
passes it through lowercased rather than guessing an exchange.
"""

from typing import ClassVar

import pandas as pd

from opendata.data.capability import Capability
from opendata.data.models import Bar
from opendata.data.protocol import FetchContext, FetchResult, Fetcher, QueryParams
from opendata.data.providers.akshare._source import SOURCE
from opendata.data.providers.akshare.models._normalize import as_date

#: Columns the sina convertible-bond frame must carry to be normalizable.
REQUIRED_COLUMNS = ("date", "open", "high", "low", "close", "volume")


class BondDailyQuery(QueryParams):
    """Validated query for the convertible-bond daily bars domain."""

    symbol: str


class AkshareBondDailyFetcher(Fetcher[BondDailyQuery, pd.DataFrame]):
    """Daily OHLCV bars for one convertible bond (e.g. ``sh010107``)."""

    capability: ClassVar[Capability] = Capability(
        asset_class="bond",
        domain="bond_daily",
        period="1D",
        market="cn",
        source=SOURCE,
        verified=False,  # B1.2: P1 sampling pending, explicit source only
    )

    def transform_query(self, **kwargs: object) -> BondDailyQuery:
        """Build and validate the query (the validate stage).

        Args:
            **kwargs: ``symbol`` (required, sina spelling such as
                ``sh010107``).

        Returns:
            The validated query.

        Raises:
            ValidationError: On unknown fields.
        """
        return BondDailyQuery.model_validate(kwargs)

    def extract_data(self, params: BondDailyQuery, ctx: FetchContext) -> pd.DataFrame:
        """Fetch the sina convertible-bond daily line (the fetch_raw stage).

        Args:
            params: Validated query.
            ctx: Per-call context; carries the timeout override.

        Returns:
            The upstream frame.
        """
        import opendata_http  # lazy: load the ported tree on routing only

        return opendata_http.bond_zh_hs_cov_daily(symbol=self.sina_code(params.symbol))

    def transform_data(self, raw: pd.DataFrame, params: BondDailyQuery) -> FetchResult:
        """Normalize the bond frame into ``Bar`` rows (the normalize stage).

        Args:
            raw: Upstream frame with :data:`REQUIRED_COLUMNS`.
            params: Validated query (symbol fallback).

        Returns:
            One ``Bar`` per trading day, ascending; rows without a
            parseable date are dropped.
        """
        frame = raw.dropna(subset=[name for name in REQUIRED_COLUMNS if name in raw.columns])
        symbol = self.sina_code(params.symbol)
        bars: list[Bar] = []
        for record in frame.to_dict("records"):
            trade_date = as_date(record.get("date"))
            if trade_date is None:
                continue
            bars.append(
                Bar(
                    symbol=symbol,
                    trade_date=trade_date,
                    open=float(record["open"]),
                    high=float(record["high"]),
                    low=float(record["low"]),
                    close=float(record["close"]),
                    volume=float(record["volume"]),
                    amount=0.0,
                )
            )
        return sorted(bars, key=lambda bar: bar.trade_date)

    @staticmethod
    def sina_code(symbol: str) -> str:
        """Canonicalize a bond symbol into the sina spelling.

        Args:
            symbol: Input code, any casing (``SH010107``, ``110059``).

        Returns:
            The lowercased code with a ``sh``/``sz`` prefix for the two
            convertible-bond code blocks (11xxxx Shanghai, 12xxxx
            Shenzhen); anything else passes through so the upstream
            error surfaces instead of a guessed exchange.
        """
        code = symbol.strip().lower()
        if code.startswith(("sh", "sz", "bj")):
            return code
        if code.startswith("11"):
            return f"sh{code}"
        if code.startswith("12"):
            return f"sz{code}"
        return code
