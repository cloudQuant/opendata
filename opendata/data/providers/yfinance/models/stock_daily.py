"""Overseas daily bars fetcher (domain ``stock_daily_overseas``, contract ``OverseasBar``).

Wraps the yfinance SDK (an **optional** dependency: importing this provider
package works without it, and routing to it fails closed with a stable code
when the SDK is absent - FR-7's isolation rule). The upstream returns OHLCV
in shares/quote-currency and no turnover, which is why the domain's contract
carries ``amount`` as nullable instead of faking zeros into the shared
``Bar`` used by the A-share sources.

Two adjustments have to be handled explicitly (all measured against the
live upstream, cross-checked against sina's published series):

* the SDK's default ``auto_adjust=True`` folds **dividend** adjustment into
  the price columns, so the seam asks for ``auto_adjust=False`` (D10);
* even in that mode the price columns are still divided by every **later**
  split ratio (and ``Volume`` multiplied by it), and that adjustment is
  computed from the full listing rather than the requested window: asking
  for AAPL 2020-06-01 .. 2020-07-14 - a window with no split event inside -
  still yields 97.0575 where the session traded 388.23. :func:`as_traded_bars`
  therefore takes the symbol's complete split history, not just the frame's
  own ``Stock Splits`` column.

Clean-room note (design §1.3): the implementation is self-written; only the
upstream SDK's public interface is referenced, no OpenBB code was consulted.
"""

from dataclasses import dataclass
from typing import ClassVar, cast

import numpy as np
import pandas as pd

from opendata.data.capability import Capability
from opendata.data.models import OverseasBar
from opendata.data.protocol import FetchContext, Fetcher, FetchResult, QueryParams
from opendata.data.providers.yfinance._source import SOURCE

#: Price columns the SDK publishes, in the source's own spelling.
PRICE_COLUMNS: tuple[str, ...] = ("Open", "High", "Low", "Close")
#: Column carrying a split's ratio on the session a split takes effect.
SPLITS_COLUMN = "Stock Splits"


def _naive_dates(index: pd.Index) -> pd.DatetimeIndex:
    """Drop the upstream's exchange tz, keeping the session dates in row order.

    Args:
        index: A frame or series index.

    Returns:
        A tz-naive, midnight-normalized index in the original order.
    """
    stamps = pd.DatetimeIndex(pd.to_datetime(index))
    if stamps.tz is not None:
        stamps = stamps.tz_localize(None)
    return cast("pd.DatetimeIndex", stamps.normalize())


def split_events(frame: pd.DataFrame, splits: pd.Series) -> pd.Series:
    """Merge the two split sources into one ``session date -> ratio`` series.

    The frame's own column comes from the same request as the prices, so a
    disagreement with the separately fetched history means one of them is
    stale or was silently truncated - both would corrupt the restoration, so
    the adapter fails closed instead of guessing.

    Args:
        frame: Upstream history frame (may carry :data:`SPLITS_COLUMN`).
        splits: The symbol's complete split history from the SDK.

    Returns:
        Ratios keyed by session date, sorted; empty when the symbol never
        split.

    Raises:
        YfinanceProviderError: A negative ratio, or a split history that
            contradicts (or is missing while the frame shows) the frame's
            own split rows.
    """
    raw_history = pd.Series(splits).astype("float64")
    if (raw_history < 0.0).any():
        raise YfinanceProviderError("YFINANCE_BAD_SPLIT_RATIO")
    history = raw_history[raw_history > 0.0].copy()
    history.index = _naive_dates(history.index)
    history = history.sort_index()
    if SPLITS_COLUMN not in frame.columns:
        return cast("pd.Series", history)
    observed = pd.Series(pd.to_numeric(frame[SPLITS_COLUMN], errors="coerce")).astype("float64")
    observed.index = _naive_dates(frame.index)
    observed = observed[observed > 0.0]
    if observed.empty:
        return cast("pd.Series", history)
    if history.empty or not observed.equals(history.reindex(observed.index).fillna(0.0)):
        raise YfinanceProviderError("YFINANCE_SPLITS_UNAVAILABLE")
    return cast("pd.Series", history)


def lookahead_factors(row_dates: pd.Index, events: pd.Series) -> pd.Series:
    """Return each session's product of *strictly later* split ratios.

    Args:
        row_dates: Session dates of the bars to restore, any order, with or
            without the upstream's exchange tz.
        events: Split ratios keyed by session date, ascending.

    Returns:
        A series keyed by the tz-naive session dates, in the order given;
        ``1.0`` from the last split on.
    """
    dates = _naive_dates(row_dates)
    if events.empty:
        return cast("pd.Series", pd.Series(1.0, index=dates))
    trailing = np.cumprod(events.values[::-1])[::-1]
    positions = events.index.searchsorted(dates, side="right")
    # A position past the last split means there is nothing left to undo.
    clamped = np.minimum(positions, len(events) - 1)
    factors = np.where(positions == len(events), 1.0, trailing[clamped])
    return cast("pd.Series", pd.Series(factors, index=dates))


@dataclass(frozen=True)
class YfinanceHistory:
    """What the seam extracts: the requested window plus the split history.

    The frame alone is not enough to restore as-traded prices, because the
    upstream's backward split adjustment looks at events after the window.
    """

    frame: pd.DataFrame
    splits: pd.Series


def as_traded_bars(frame: pd.DataFrame, splits: pd.Series) -> pd.DataFrame:
    """Undo the upstream's backward split adjustment on the OHLCV columns.

    Args:
        frame: Upstream history frame keyed by a trading-date index.
        splits: The symbol's complete split history.

    Returns:
        A copy whose prices are as-traded (multiplied by the later-split
        factor) and whose volume is as-traded (divided by it).
    """
    events = split_events(frame, splits)
    factor = lookahead_factors(frame.index, events)
    restored = cast("pd.DataFrame", frame.copy())
    for column in PRICE_COLUMNS:
        restored[column] = frame[column].to_numpy(dtype="float64") * factor.to_numpy()
    restored["Volume"] = frame["Volume"].to_numpy(dtype="float64") / factor.to_numpy()
    return restored


def settled_rows(frame: pd.DataFrame) -> pd.DataFrame:
    """Drop the rows whose prices are not settled yet.

    A window that reaches the current session gets a bar with ``NaN`` prices
    and a partial volume (measured: ~1.5M shares for AAPL, and it can carry
    the *previous* session's date). It is not a trading result, so it must not
    be stored or rejected over.

    Args:
        frame: Restored (as-traded) upstream frame.

    Returns:
        The rows with a complete OHLC set.
    """
    return cast("pd.DataFrame", frame.dropna(subset=list(PRICE_COLUMNS)))


class StockDailyOverseasQuery(QueryParams):
    """Validated query for the overseas daily bars domain."""

    symbol: str


class YfinanceStockDailyFetcher(Fetcher[StockDailyOverseasQuery, YfinanceHistory]):
    """Daily OHLCV bars for one overseas symbol (unadjusted, D10).

    ``verified`` is set because the restored series was compared against an
    independent vendor on live data, straddling a 4:1 and a 5:1 split (R2:
    implement first, verify against the official values, see
    ``docs/evidence/C8/``).
    """

    async_mode = "bounded_thread"

    capability: ClassVar[Capability] = Capability(
        asset_class="equity",
        domain="stock_daily_overseas",
        period="1D",
        market="global",
        source=SOURCE,
        verified=True,
    )

    def transform_query(self, **kwargs: object) -> StockDailyOverseasQuery:
        """Build and validate the query (the validate stage).

        Args:
            **kwargs: ``symbol`` (required, upstream ticker such as ``AAPL``),
                ``start_date``, ``end_date``.

        Returns:
            The validated query.

        Raises:
            ValidationError: On unknown fields or a blank symbol.
        """
        return StockDailyOverseasQuery.model_validate(kwargs)

    def extract_data(self, params: StockDailyOverseasQuery, ctx: FetchContext) -> YfinanceHistory:
        """Fetch the upstream window and the split history (the fetch_raw stage).

        Args:
            params: Validated query.
            ctx: Per-call context; carries the timeout override.

        Returns:
            The requested window in the SDK's raw mode plus the symbol's full
            split history (the window alone cannot be de-adjusted).

        Raises:
            YfinanceProviderError: The SDK is not installed, or the upstream
                returned no rows for the request.
        """
        from opendata.data.providers.yfinance.models import _sdk

        frame = _sdk.require_history_frame(
            params.symbol,
            start=params.start_date,
            end=params.end_date,
            timeout=ctx.timeout,
        )
        if frame is None or frame.empty:
            raise YfinanceProviderError("YFINANCE_EMPTY_RESPONSE")
        splits = _sdk.require_split_history(params.symbol)
        return YfinanceHistory(frame=frame, splits=splits)

    def transform_data(self, raw: YfinanceHistory, params: StockDailyOverseasQuery) -> FetchResult:
        """Normalize the upstream frame (the normalize stage).

        Restores the as-traded OHLCV (:func:`as_traded_bars`), drops the
        unfinished session bar, then maps the SDK's columns onto
        ``OverseasBar`` rows; ``amount`` stays ``None`` because the upstream
        provides no turnover (it is nullable on this contract by design).

        Args:
            raw: Upstream window plus split history.
            params: The validated query.

        Returns:
            Contract rows sorted by trading date.

        Raises:
            YfinanceProviderError: A required upstream column is missing.
        """
        missing = [name for name in (*PRICE_COLUMNS, "Volume") if name not in raw.frame.columns]
        if missing:
            raise YfinanceProviderError("YFINANCE_COLUMNS_MISSING")
        frame = (
            settled_rows(as_traded_bars(raw.frame, raw.splits))
            .rename_axis("Date")
            .reset_index()
            .rename(
                columns={
                    "Date": "trade_date",
                    "Open": "open",
                    "High": "high",
                    "Low": "low",
                    "Close": "close",
                    "Volume": "volume",
                }
            )
        )
        frame["symbol"] = params.symbol.strip().upper()
        frame["amount"] = None
        rows = OverseasBar.from_frame(frame)
        return tuple(sorted(rows, key=lambda bar: bar.trade_date))


class YfinanceProviderError(RuntimeError):
    """Stable failures of the yfinance provider adapter."""

    def __init__(self, code: str) -> None:
        """Store the stable failure code (and use it as the message).

        Args:
            code: One of the provider's stable codes, for example
                ``YFINANCE_SDK_MISSING``.
        """
        self.code = code
        super().__init__(code)
