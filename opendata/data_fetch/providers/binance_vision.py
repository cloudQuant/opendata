"""Binance public market-data client.

Binance's primary API domains are DNS-polluted and SNI-blocked from mainland
networks.  Two official public endpoints remain reachable and need no API key:

``data-api.binance.vision``  REST market data (klines, 24h ticker), rate-limited
                             by request weight (2000/minute).
``data.binance.vision``      S3-style CSV archives of historical klines, one file
                             per month (per day for the open month) -- no weight
                             limit, so bulk backfills should prefer these.
"""

from __future__ import annotations

import io
import json
import logging
import re
import time
import urllib.error
import urllib.request
import zipfile
from collections.abc import Iterator
from datetime import date, datetime, timedelta, timezone
from typing import Any

MARKET_DATA_HOST = "https://data-api.binance.vision"
ARCHIVE_HOST = "https://data.binance.vision"

# Binance klines are unnamed positional columns; index 11 ("ignore") is unused.
KLINE_COLUMNS = (
    "open_time",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "close_time",
    "quote_volume",
    "trade_count",
    "taker_buy_volume",
    "taker_buy_quote_volume",
)
# Binance switched the archive CSV epochs from milliseconds to microseconds
# between 2024-01 and 2026-01; both scales still appear, so classify by
# magnitude instead of by file scope.
_MICROSECOND_CUTOVER = 100_000_000_000_000
_SECOND_CUTOVER = 100_000_000_000

INTERVAL_MS = {
    "1m": 60_000,
    "5m": 300_000,
    "15m": 900_000,
    "30m": 1_800_000,
    "1h": 3_600_000,
    "2h": 7_200_000,
    "4h": 14_400_000,
    "1d": 86_400_000,
    "1w": 604_800_000,
}

MAX_KLINES_PER_REQUEST = 1000
WEIGHT_BUDGET_PER_MINUTE = 1800

_SYMBOL_RE = re.compile(r"^[A-Z0-9]{5,20}$")
_MONTH_RE = re.compile(r"^\d{4}-\d{2}$")
_DAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class BinanceRequestError(RuntimeError):
    """A Binance request that could not be completed after retries."""


class BinanceClientError(RuntimeError):
    """A non-retryable 4xx rejection (bad symbol, bad interval)."""

    def __init__(self, status: int, detail: str) -> None:
        super().__init__(f"HTTP {status}: {detail}")
        self.status = status


def _read_http_error(exc: urllib.error.HTTPError) -> str:
    try:
        return exc.read().decode("utf-8", errors="replace")[:200]
    except Exception:
        return str(exc)


def validate_symbol(symbol: str) -> str:
    value = symbol.strip().upper()
    if not _SYMBOL_RE.match(value):
        raise ValueError(f"invalid Binance symbol: {symbol!r}")
    return value


def validate_interval(interval: str) -> str:
    value = interval.strip().lower()
    if value not in INTERVAL_MS:
        raise ValueError(f"unsupported interval: {interval!r}")
    return value


def date_to_ms(value: date, *, end_of_day: bool = False) -> int:
    """Midnight (or end-of-day) UTC for ``value`` as Binance epoch millis."""
    moment = datetime(
        value.year,
        value.month,
        value.day,
        23 if end_of_day else 0,
        59 if end_of_day else 0,
        tzinfo=timezone.utc,
    )
    return int(moment.timestamp() * 1000)


def to_epoch_ms(value: Any) -> int:
    """Normalise a Binance epoch to milliseconds regardless of source scale."""
    raw = int(float(value))
    if raw >= _MICROSECOND_CUTOVER:
        return raw // 1000
    if raw >= _SECOND_CUTOVER:
        return raw
    return raw * 1000


def to_bar(cells: list[str] | list[Any]) -> dict[str, Any]:
    """Map one positional kline (REST list or CSV row) onto named columns."""
    bar: dict[str, Any] = {}
    for index, column in enumerate(KLINE_COLUMNS):
        if index >= len(cells):
            break
        raw = cells[index]
        if column in ("open_time", "close_time"):
            bar[column] = to_epoch_ms(raw)
        elif column == "trade_count":
            bar[column] = int(str(raw))
        else:
            bar[column] = float(raw)
    bar["trade_time"] = bar_datetime(bar["open_time"])
    return bar


def bar_datetime(open_time_ms: int) -> datetime:
    """Bar open time in UTC, naive -- the warehouse stores UTC everywhere."""
    return datetime.fromtimestamp(to_epoch_ms(open_time_ms) / 1000, tz=timezone.utc).replace(
        tzinfo=None
    )


def _month_bounds(year_month: str) -> tuple[date, date]:
    first = datetime.strptime(year_month, "%Y-%m").date()
    last_day = (
        date(first.year + 1, 1, 1) - timedelta(days=1)
        if first.month == 12
        else date(first.year, first.month + 1, 1) - timedelta(days=1)
    )
    return first, last_day


def iter_month_windows(start: date, end: date | None = None) -> Iterator[str]:
    ceiling = end or date.today()
    cursor = date(start.year, start.month, 1)
    while cursor <= ceiling:
        yield cursor.strftime("%Y-%m")
        cursor = (
            date(cursor.year + 1, 1, 1)
            if cursor.month == 12
            else date(cursor.year, cursor.month + 1, 1)
        )


def _http_get(
    url: str,
    *,
    timeout: float,
    logger: logging.Logger,
    max_attempts: int = 5,
) -> bytes:
    """GET with backoff.

    This network intermittently presents a certificate issued for another
    hostname on the Binance mirror, so TLS failures are retried rather than
    fatal.  Verification is never disabled -- a wrong- cert response is
    discarded, not accepted.
    """
    request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    last_error: Exception | None = None
    for attempt in range(max_attempts):
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:  # nosec B310
                return response.read()
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                raise
            if 400 <= exc.code < 500 and exc.code not in (418, 429):
                # bad symbol / bad interval -- fail fast so one bad code cannot
                # stall a whole backfill behind retries
                raise BinanceClientError(exc.code, _read_http_error(exc)) from exc
            last_error = exc
            if exc.code in (418, 429):
                try:
                    retry_after = int(str(exc.headers.get("Retry-After")))
                except (TypeError, ValueError):
                    retry_after = None
                pause = min(retry_after or 60, 300)
                logger.warning("Binance HTTP %s, sleeping %ss", exc.code, pause)
                time.sleep(pause)
            else:
                time.sleep(min(2**attempt, 30))
        except Exception as exc:  # transient timeout / reset / TLS interception
            last_error = exc
            time.sleep(min(1.5**attempt, 20))
    raise BinanceRequestError(f"GET {url} failed after {max_attempts} attempts: {last_error}")


class BinanceClient:
    """Read-only Binance spot market data: live bars, snapshot, CSV archives."""

    def __init__(
        self,
        *,
        timeout: float = 20.0,
        logger: logging.Logger | None = None,
        min_request_gap: float = 0.15,
    ) -> None:
        self.timeout = timeout
        self.logger = logger or logging.getLogger("binance_vision")
        self.min_request_gap = min_request_gap
        self._last_request_at = 0.0
        self._minute_started_at = 0.0
        self._used_weight = 0

    # -- REST ---------------------------------------------------------------

    def _throttle(self, expected_weight: int) -> None:
        now = time.monotonic()
        if now - self._minute_started_at >= 60:
            self._minute_started_at = now
            self._used_weight = 0
        if self._used_weight + expected_weight > WEIGHT_BUDGET_PER_MINUTE:
            pause = 60 - (now - self._minute_started_at) + 0.5
            if pause > 0:
                self.logger.info("request weight budget reached, sleeping %.1fs", pause)
                time.sleep(pause)
            self._minute_started_at = time.monotonic()
            self._used_weight = 0
        gap = self.min_request_gap - (time.monotonic() - self._last_request_at)
        if gap > 0:
            time.sleep(gap)
        self._last_request_at = time.monotonic()

    def _get_json(self, path: str, *, weight: int) -> Any:
        self._throttle(weight)
        payload = _http_get(f"{MARKET_DATA_HOST}{path}", timeout=self.timeout, logger=self.logger)
        self._used_weight += weight
        return json.loads(payload)

    def fetch_klines(
        self,
        symbol: str,
        interval: str,
        *,
        start_ms: int | None = None,
        end_ms: int | None = None,
        limit: int = MAX_KLINES_PER_REQUEST,
    ) -> list[dict[str, Any]]:
        """One page of klines, oldest first."""
        pair = validate_symbol(symbol)
        step = validate_interval(interval)
        query = f"symbol={pair}&interval={step}&limit={min(int(limit), MAX_KLINES_PER_REQUEST)}"
        if start_ms is not None:
            query += f"&startTime={int(start_ms)}"
        if end_ms is not None:
            query += f"&endTime={int(end_ms)}"
        return [to_bar(row) for row in self._get_json(f"/api/v3/klines?{query}", weight=5)]

    def iter_klines(
        self,
        symbol: str,
        interval: str,
        *,
        since_ms: int,
        until_ms: int | None = None,
    ) -> Iterator[dict[str, Any]]:
        """Yield bars within [since_ms, until_ms], advancing by bar length.

        ``endTime`` is sent to the API *and* re-checked locally: Binance returns
        up to ``limit`` bars starting at ``startTime`` regardless of ``endTime``,
        so without the local clip the caller silently over-fetches past the
        requested window.
        """
        step = validate_interval(interval)
        cursor = int(since_ms)
        ceiling = int(until_ms) if until_ms is not None else int(time.time() * 1000)
        while cursor < ceiling:
            page = self.fetch_klines(symbol, interval, start_ms=cursor, end_ms=ceiling)
            if not page:
                return
            emitted = 0
            for bar in page:
                if bar["open_time"] > ceiling:
                    return
                yield bar
                emitted += 1
            if len(page) < MAX_KLINES_PER_REQUEST or emitted == 0:
                return
            cursor = page[-1]["open_time"] + INTERVAL_MS[step]

    def fetch_snapshot(self, symbol: str) -> dict[str, Any]:
        """24h rolling stats -- the live replacement for ``ak.crypto_js_spot``."""
        pair = validate_symbol(symbol)
        return self._get_json(f"/api/v3/ticker/24hr?symbol={pair}", weight=2)

    def latest_bar(self, symbol: str, interval: str) -> dict[str, Any] | None:
        page = self.fetch_klines(symbol, interval, limit=1)
        return page[-1] if page else None

    # -- archives -----------------------------------------------------------

    @staticmethod
    def _archive_url(scope: str, pair: str, interval: str, window: str) -> str:
        return f"{ARCHIVE_HOST}/data/spot/{scope}/klines/{pair}/{interval}/{pair}-{interval}-{window}.zip"

    def _fetch_archive(
        self, scope: str, pair: str, interval: str, window: str
    ) -> list[dict[str, Any]]:
        url = self._archive_url(scope, pair, interval, window)
        try:
            blob = _http_get(url, timeout=self.timeout, logger=self.logger)
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return []
            raise BinanceRequestError(f"archive {url} failed: {exc}") from exc
        except BinanceRequestError as exc:
            self.logger.warning("archive unavailable: %s", exc)
            return []
        with zipfile.ZipFile(io.BytesIO(blob)) as archive:
            names = archive.namelist()
            if not names:
                return []
            text = archive.read(names[0]).decode("utf-8", errors="replace")
        bars: list[dict[str, Any]] = []
        for line in text.strip().splitlines():
            cells = line.split(",")
            if not cells or not cells[0].lstrip("-").isdigit():
                continue  # header row (some windows carry one) or blank tail
            bars.append(to_bar(cells))
        return bars

    def fetch_month_bars(self, symbol: str, interval: str, year_month: str) -> list[dict[str, Any]]:
        """Closed-month archive. Empty when the month has not rolled up yet."""
        if not _MONTH_RE.match(year_month):
            raise ValueError(f"invalid month window: {year_month!r}")
        return self._fetch_archive(
            "monthly", validate_symbol(symbol), validate_interval(interval), year_month
        )

    def fetch_day_bars(self, symbol: str, interval: str, day: str) -> list[dict[str, Any]]:
        if not _DAY_RE.match(day):
            raise ValueError(f"invalid day window: {day!r}")
        return self._fetch_archive(
            "daily", validate_symbol(symbol), validate_interval(interval), day
        )

    def iter_archive_bars(
        self,
        symbol: str,
        interval: str,
        *,
        start: date,
        end: date | None = None,
    ) -> Iterator[dict[str, Any]]:
        """Archive-first walk: monthly files, daily files for the open month."""
        ceiling = end or date.today()
        step = validate_interval(interval)
        pair = validate_symbol(symbol)
        for year_month in iter_month_windows(start, ceiling):
            first, last = _month_bounds(year_month)
            window_start = max(first, start)
            window_end = min(last, ceiling)
            bars = self._fetch_archive("monthly", pair, step, year_month)
            if bars:
                yield from (
                    bar for bar in bars if window_start <= bar["trade_time"].date() <= window_end
                )
                continue
            cursor = window_start
            while cursor <= window_end:
                yield from self._fetch_archive("daily", pair, step, cursor.strftime("%Y-%m-%d"))
                cursor += timedelta(days=1)
