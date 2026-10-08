"""The sina-backed akshare legs must honor the window they were asked for (C24).

Four akshare fetchers wrap upstream pages that return a symbol's whole
history and take no date argument - sina's futures, option and
convertible-bond daily lines and the sina dividend/rights pages. They
inherited the same hole: ``start_date``/``end_date`` validated, then ignored.
Measured on 2026-09-25, a request for the 16 trading days ending 2026-09-25
came back with 210 bars on ``futures_daily``, 107 on ``option_daily`` and
1,415 rows spanning six years on ``bond_daily``. So an incremental job that
names a window would rewrite history it never asked for, and a degradation
to one of these legs would not be answering the same question.

The fix lives in the normalize stage (:func:`within_window`), so these tests
stub the extraction stage with a wide frame and assert on what ``fetch``
hands back: no row outside the window, and - the other half of the contract
- the full history still comes back when no window was requested.
"""

from __future__ import annotations

import importlib
import socket
import subprocess
import sys
from datetime import date
from typing import TYPE_CHECKING, Any, NoReturn

import pandas as pd
import pytest
from requests import Request
from requests.sessions import Session

from opendata.data.providers import register_providers
from opendata.data.providers.akshare.models._normalize import within_window
from opendata.data.registry import get_registry

if TYPE_CHECKING:
    from collections.abc import Callable

    from pytest import MonkeyPatch

    from opendata.data.protocol import Fetcher

#: Inclusive bounds of the window every case asks for.
WINDOW_START = date(2026, 9, 7)
WINDOW_END = date(2026, 9, 18)

#: Every calendar day of September 2026 - the synthetic upstream's span.
FRAME_DAYS = tuple(date(2026, 9, day) for day in range(1, 31))

#: The days of that frame inside the window (the expected answer).
IN_WINDOW_DAYS = tuple(day for day in FRAME_DAYS if WINDOW_START <= day <= WINDOW_END)

#: Chinese spelling of a sina daily line (the option page).
_CN_COLUMNS = ("日期", "开盘", "最高", "最低", "收盘", "成交量")
#: English spelling of a sina daily line (the futures and bond pages).
_EN_COLUMNS = ("date", "open", "high", "low", "close", "volume")


def _daily_frame(columns: tuple[str, ...]) -> pd.DataFrame:
    """One daily-line row per September day under the given column names."""
    rows = []
    for index, day in enumerate(FRAME_DAYS):
        values = (
            day.isoformat(),
            float(index),
            float(index) + 1.0,
            float(index) - 1.0,
            float(index),
            float(index) * 10.0,
        )
        rows.append(dict(zip(columns, values, strict=True)))
    return pd.DataFrame(rows)


def _action_frame() -> pd.DataFrame:
    """A sina 分红 page: one dividend row per September day."""
    return pd.DataFrame(
        [
            {
                "除权除息日": day.isoformat(),
                "派息": 1.0,
                "送股": 0.0,
                "转增": 0.0,
                "indicator": "分红",
            }
            for day in FRAME_DAYS
        ]
    )


#: Legs that read a request window but whose upstream cannot: they must crop
#: in normalize. ``(domain, contract date field, frame factory)``; the set is
#: pinned by :func:`test_every_akshare_leg_is_triaged_by_what_it_asks_upstream`.
WINDOW_BLIND_LEGS: tuple[tuple[str, str, Callable[[], pd.DataFrame]], ...] = (
    ("futures_daily", "trade_date", lambda: _daily_frame(_EN_COLUMNS)),
    ("option_daily", "trade_date", lambda: _daily_frame(_CN_COLUMNS)),
    ("bond_daily", "trade_date", lambda: _daily_frame(_EN_COLUMNS)),
    ("stock_action", "ex_date", _action_frame),
)

#: The three eastmoney kline routes push the window upstream (``beg``/``end``),
#: so normalize sees an already-cropped frame.
UPSTREAM_WINDOWED_LEGS = frozenset({"stock_daily", "index_daily", "fund_etf_daily"})

#: Report-period and snapshot tables: a request window is not how they are
#: keyed, so extraction naming no window is the expected shape, not a hole.
PERIOD_KEYED_LEGS = frozenset({"financial_indicator", "financial_statement", "index_constituent"})

_AKSHARE_PACKAGE = "opendata.data.providers.akshare"
_AKSHARE_VENDOR = f"{_AKSHARE_PACKAGE}._vendor"

LEG_CASES = pytest.mark.parametrize(
    ("domain", "date_attr", "frame_factory"),
    WINDOW_BLIND_LEGS,
    ids=[leg[0] for leg in WINDOW_BLIND_LEGS],
)


def _fetcher(domain: str) -> Fetcher[Any, Any]:
    """The akshare fetcher the live registry holds for one domain."""
    register_providers()
    registry = get_registry()
    capability = next(cap for cap in registry.capabilities() if cap.domain == domain)
    return registry.resolve(capability.asset_class, domain, source="akshare")


def _stubbed(monkeypatch: MonkeyPatch, domain: str, frame: pd.DataFrame) -> Fetcher[Any, Any]:
    """Point one fetcher's extraction at a recorded frame, for this test only."""
    fetcher = _fetcher(domain)
    monkeypatch.setattr(fetcher, "extract_data", lambda params, ctx: frame)
    return fetcher


class _OutboundRecorder:
    """Stand in for the canonical ported AKShare vendor package."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def __getattr__(self, name: str) -> Callable[..., pd.DataFrame]:
        """Answer any upstream function with an empty frame, after recording it."""
        if name.startswith("__"):
            raise AttributeError(name)

        def call(*_args: Any, **kwargs: Any) -> pd.DataFrame:
            self.calls.append((name, kwargs))
            return pd.DataFrame()

        return call


class _OutboundBlockedError(RuntimeError):
    """Raised when the test reaches any real outbound transport path."""


class _OutboundTripwire:
    """Record and reject network/process paths before they can send anything."""

    def __init__(self) -> None:
        self.attempts: list[str] = []

    def block(self, operation: str) -> NoReturn:
        self.attempts.append(operation)
        raise _OutboundBlockedError(f"unexpected outbound attempt through {operation}")

    def socket_connect(self, _sock: Any, address: Any) -> NoReturn:
        self.block(f"socket.connect({address!r})")

    def socket_connect_ex(self, _sock: Any, address: Any) -> NoReturn:
        self.block(f"socket.connect_ex({address!r})")

    def create_connection(self, address: Any, *_args: Any, **_kwargs: Any) -> NoReturn:
        self.block(f"socket.create_connection({address!r})")

    def getaddrinfo(self, host: Any, *_args: Any, **_kwargs: Any) -> NoReturn:
        self.block(f"socket.getaddrinfo({host!r})")

    def requests_send(self, _session: Any, *_args: Any, **_kwargs: Any) -> NoReturn:
        self.block("requests.Session.send")

    def subprocess_run(self, *_args: Any, **_kwargs: Any) -> NoReturn:
        self.block("subprocess.run")


def _install_outbound_tripwire(monkeypatch: MonkeyPatch) -> _OutboundTripwire:
    """Make every common HTTP and socket path fail before network I/O."""
    tripwire = _OutboundTripwire()

    def socket_connect(sock: Any, address: Any) -> NoReturn:
        tripwire.socket_connect(sock, address)

    def socket_connect_ex(sock: Any, address: Any) -> NoReturn:
        tripwire.socket_connect_ex(sock, address)

    monkeypatch.setattr(socket.socket, "connect", socket_connect)
    monkeypatch.setattr(socket.socket, "connect_ex", socket_connect_ex)
    monkeypatch.setattr(socket, "create_connection", tripwire.create_connection)
    monkeypatch.setattr(socket, "getaddrinfo", tripwire.getaddrinfo)
    monkeypatch.setattr(Session, "send", tripwire.requests_send)
    monkeypatch.setattr(subprocess, "run", tripwire.subprocess_run)
    return tripwire


def test_outbound_tripwire_rejects_transport_paths_before_sending(
    monkeypatch: MonkeyPatch,
) -> None:
    """The test guard rejects socket, requests, and curl paths without sending."""
    tripwire = _install_outbound_tripwire(monkeypatch)

    with socket.socket() as sock, pytest.raises(_OutboundBlockedError, match="socket.connect"):
        sock.connect(("192.0.2.1", 80))

    request = Request("GET", "https://example.invalid").prepare()
    with Session() as session, pytest.raises(_OutboundBlockedError, match="requests.Session.send"):
        session.send(request)

    with pytest.raises(_OutboundBlockedError, match="subprocess.run"):
        subprocess.run([sys.executable, "-c", "pass"], check=False)

    assert len(tripwire.attempts) == 3


def _window_reaches_upstream(
    domain: str,
    monkeypatch: MonkeyPatch,
    tripwire: _OutboundTripwire,
) -> bool:
    """Ask one leg for the window and watch whether the dates left through upstream.

    Args:
        domain: The akshare domain to probe.
        monkeypatch: pytest fixture, used to swap the canonical vendor package.
        tripwire: Test-local guard that prevents real outbound requests.

    Returns:
        True when an outbound call carried exactly this start/end pair.
    """
    recorder = _OutboundRecorder()
    package = importlib.import_module(_AKSHARE_PACKAGE)
    vendor = importlib.import_module(_AKSHARE_VENDOR)
    assert sys.modules[_AKSHARE_VENDOR] is vendor
    assert package._vendor is vendor
    monkeypatch.setitem(sys.modules, _AKSHARE_VENDOR, recorder)
    monkeypatch.setattr(package, "_vendor", recorder)
    assert sys.modules[_AKSHARE_VENDOR] is recorder
    assert package._vendor is recorder

    fetcher = _fetcher(domain)
    # 空帧会让某些腿在 normalize 处 fail-closed（financial_indicator 就是这样）：
    # 要读的是出站调用，它在 normalize 之前就已经发生了。
    try:
        list(fetcher.fetch(symbol="10011425", start_date=WINDOW_START, end_date=WINDOW_END))
    except _OutboundBlockedError:
        raise
    except Exception:
        pass

    assert not tripwire.attempts, (
        f"{domain} escaped the patched AKShare vendor namespace and reached "
        f"the outbound tripwire: {tripwire.attempts}"
    )
    assert recorder.calls, f"{domain} did not call the patched canonical AKShare vendor package"
    return any(_carries_window(kwargs) for _, kwargs in recorder.calls)


def _carries_window(kwargs: dict[str, Any]) -> bool:
    """True when one outbound call carried exactly this window, in either spelling."""
    spellings = {
        (WINDOW_START.isoformat(), WINDOW_END.isoformat()),
        (WINDOW_START.strftime("%Y%m%d"), WINDOW_END.strftime("%Y%m%d")),
        (WINDOW_START, WINDOW_END),
    }

    return (kwargs.get("start_date"), kwargs.get("end_date")) in spellings


@LEG_CASES
def test_a_named_window_crops_the_whole_history(
    monkeypatch: MonkeyPatch,
    domain: str,
    date_attr: str,
    frame_factory: Callable[[], pd.DataFrame],
) -> None:
    """A windowed request returns exactly its window - sina handed over the rest."""
    fetcher = _stubbed(monkeypatch, domain, frame_factory())

    rows = list(fetcher.fetch(symbol="10011425", start_date=WINDOW_START, end_date=WINDOW_END))

    days = [getattr(row, date_attr) for row in rows]
    assert days == sorted(days), f"{domain} must stay date-ascending"
    assert set(days) == set(IN_WINDOW_DAYS), (
        f"{domain} answered {len(days)} rows for a {len(IN_WINDOW_DAYS)}-day window"
    )


@LEG_CASES
def test_an_open_request_still_returns_the_full_history(
    monkeypatch: MonkeyPatch,
    domain: str,
    date_attr: str,
    frame_factory: Callable[[], pd.DataFrame],
) -> None:
    """With no window given, the crop must not invent a range of its own."""
    fetcher = _stubbed(monkeypatch, domain, frame_factory())

    rows = list(fetcher.fetch(symbol="10011425"))

    assert len(rows) == len(FRAME_DAYS)


def test_one_sided_windows_bound_only_the_side_given(monkeypatch: MonkeyPatch) -> None:
    """Each bound is independent: naming one must not close the other."""
    fetcher = _stubbed(monkeypatch, "futures_daily", _daily_frame(_EN_COLUMNS))

    tail = [bar.trade_date for bar in fetcher.fetch(symbol="CU2611", start_date=WINDOW_END)]
    head = [bar.trade_date for bar in fetcher.fetch(symbol="CU2611", end_date=WINDOW_START)]

    assert tail == [day for day in FRAME_DAYS if day >= WINDOW_END]
    assert head == [day for day in FRAME_DAYS if day <= WINDOW_START]


def test_within_window_bounds_are_inclusive() -> None:
    """Both ends are inside the window; a None side leaves it open."""
    assert within_window(date(2026, 9, 7), date(2026, 9, 7), date(2026, 9, 18))
    assert within_window(date(2026, 9, 18), date(2026, 9, 7), date(2026, 9, 18))
    assert not within_window(date(2026, 9, 6), date(2026, 9, 7), date(2026, 9, 18))
    assert not within_window(date(2026, 9, 19), date(2026, 9, 7), date(2026, 9, 18))
    assert within_window(date(2026, 9, 19), None, None)


def test_every_akshare_leg_is_triaged_by_what_it_asks_upstream(
    monkeypatch: MonkeyPatch,
) -> None:
    """A new akshare leg must be triaged here, not born silently window-blind.

    The classification is what the leg *does*, not what its source text says:
    the outbound call is recorded, so a leg counts as upstream-windowed only
    if the window it was handed actually left through the ported akshare
    layer. Registering another akshare capability that neither pushes the
    window nor crops it in normalize fails here until it is added to
    :data:`WINDOW_BLIND_LEGS` (proven by the frame cases above), shown to be
    upstream-windowed, or shown to be a period-keyed table.
    """
    register_providers()
    registry = get_registry()
    akshare_domains = {cap.domain for cap in registry.capabilities() if cap.source == "akshare"}
    tripwire = _install_outbound_tripwire(monkeypatch)
    pushed = {
        domain
        for domain in sorted(akshare_domains)
        if _window_reaches_upstream(domain, monkeypatch, tripwire)
    }

    assert pushed == UPSTREAM_WINDOWED_LEGS
    assert akshare_domains == pushed | {leg[0] for leg in WINDOW_BLIND_LEGS} | PERIOD_KEYED_LEGS
    assert not tripwire.attempts
