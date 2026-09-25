"""Source health patrol (B3.4 / AC-4 "P0 域接口每日健康巡检").

An *active* counterpart to the registry's passive health marks: probes
each registered verified capability with a tiny real query, updates the
registry's health so auto routing skips a broken source, and reports
the outcome together with the key configuration of every source.

The probe parameters are per ``(domain, source)`` and deliberately
minimal - one symbol or one series - because a patrol that must assemble
a full query would be as fragile as the sources it checks. They are
nevertheless *real* parameters: a probe that fails validation is
reported as an unhealthy source, which quietly removes that source from
``auto`` routing (C15 audit: 12 of 19 verified capabilities were
mis-probed this way).
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from datetime import date, timedelta
from typing import TYPE_CHECKING, cast

from loguru import logger

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence
    from typing import Any

    from opendata.data.capability import Capability
    from opendata.data.models import Instrument
    from opendata.data.protocol import Fetcher
    from opendata.data.registry import ProviderRegistry

#: Per-second timeout of one probe. Sized to the *slowest* verified source
#: rather than the fastest: measured over two runs on 2026-09-25, IMF
#: DataMapper answers the same annual query in 8.7-11.6s, so a 10s ceiling
#: reported a healthy IMF leg as down on every run it answered late.
PROBE_TIMEOUT = 30.0
#: Probe parameters keyed by ``(domain, source)``; a verified capability
#: without an entry cannot be probed. Every window is a *fixed* historical
#: range on purpose - a range the sources have already published can never
#: expire, while "the last N days" reintroduces the weekend/holiday false
#: alarms and the rolling-coverage limits of the calendar endpoint.
#: ``index_daily`` probes a *bare* index code on purpose: it is the only probe
#: that walks ``resolve_index_code`` (index catalog lookup) before fetching,
#: while ``index_constituent`` stays qualified so the pass-the-symbol-through
#: path is covered too and a catalog outage does not fail both legs alike.
ProbeParams = dict[str, str | date]
PROBE_PARAMS: dict[tuple[str, str], ProbeParams] = {
    ("economy_cpi", "ecb"): {
        "series_id": "ICP/M.U2.N.000000.4.ANR",
        "start_date": date(2024, 1, 1),
        "end_date": date(2024, 12, 31),
    },
    ("economy_gdp", "ecb"): {
        "series_id": "MNA/Q.N.I9.W2.S1.S1.B.B1GQ._Z._Z._Z.EUR.LR.N",
        "start_date": date(2023, 1, 1),
        "end_date": date(2024, 12, 31),
    },
    ("economy_rate", "ecb"): {
        "series_id": "FM/B.U2.EUR.4F.KR.MRR_FR.LEV",
        "start_date": date(2022, 1, 1),
        "end_date": date(2024, 12, 31),
    },
    ("economy_cpi", "imf"): {
        "indicator": "PCPIPCH",
        "country": "USA",
        "start_date": date(2015, 1, 1),
        "end_date": date(2024, 12, 31),
    },
    ("economy_gdp", "imf"): {
        "indicator": "NGDP_RPCH",
        "country": "USA",
        "start_date": date(2015, 1, 1),
        "end_date": date(2024, 12, 31),
    },
    ("economy_unemployment", "imf"): {
        "indicator": "LUR",
        "country": "USA",
        "start_date": date(2015, 1, 1),
        "end_date": date(2024, 12, 31),
    },
    ("economy_cpi", "oecd"): {
        "series_key": "GBR.M.HICP.CPI.PC.CP09.N.G1",
        "start_date": date(2023, 1, 1),
        "end_date": date(2024, 12, 31),
    },
    ("economy_unemployment", "oecd"): {
        "series_key": "USA.UNE_RATE.PT_LF_SUB._T.Y15T64.UNE",
        "start_date": date(2023, 1, 1),
        "end_date": date(2024, 12, 31),
    },
    ("stock_daily", "ths"): {
        "symbol": "600519",
        "start_date": date(2024, 9, 2),
        "end_date": date(2024, 9, 6),
    },
    ("stock_action", "ths"): {
        "symbol": "600519",
        "start_date": date(2023, 1, 1),
        "end_date": date(2024, 12, 31),
    },
    ("index_daily", "ths"): {
        "symbol": "000300",
        "start_date": date(2024, 9, 2),
        "end_date": date(2024, 9, 6),
    },
    ("index_constituent", "ths"): {"symbol": "000300.SH"},
    ("financial_statement", "ths"): {"symbol": "600519"},
    ("fund_action", "ths"): {
        "symbol": "510300",
        "start_date": date(2023, 1, 1),
        "end_date": date(2025, 12, 31),
    },
    ("instrument", "ths"): {"asset_type": "a-share"},
    ("trading_calendar", "ths"): {},
    ("stock_daily_overseas", "yfinance"): {
        "symbol": "AAPL",
        "start_date": date(2024, 9, 2),
        "end_date": date(2024, 9, 6),
    },
}

#: Product whose contract ladder is continuous enough to name from a date
#: (SHFE copper lists every nearby month), and how far ahead of today the
#: contract month sits so the named contract is still listed.
FUTURES_PROBE_PRODUCT = "CU"
FUTURES_PROBE_LEAD_MONTHS = 2
#: Option contracts must stay listed this long after the probe date, which
#: keeps the picked one actively traded; the code is read from the
#: instrument catalog because option thscodes are opaque.
OPTION_PROBE_HORIZON_DAYS = 60
#: Days of history the rolling probes ask for (a contract's life is longer
#: than this even over a holiday week).
ROLLING_PROBE_WINDOW_DAYS = 21
#: Domain whose catalog supplies the live option contract.
OPTION_PROBE_CATALOG = ("instrument", "ths")
#: Fund the ETF daily probe names: it needs no derivation, only a window.
FUND_ETF_PROBE_SYMBOL = "510300"


class PatrolProbeError(RuntimeError):
    """A probe could not be assembled or returned nothing to check."""


class PatrolProbeConfigError(PatrolProbeError):
    """The patrol has no probe for a verified capability.

    Distinct from a probe failure: the source is not known to be broken,
    so the patrol reports the gap without touching routing health.
    """


@dataclass(frozen=True)
class PatrolResult:
    """Outcome of probing one capability.

    Attributes:
        domain: Domain identifier.
        source: Source identifier.
        ok: Whether the probe call succeeded.
        error: Failure detail (None on success).
        latency_ms: Probe duration in milliseconds.
        verified: Whether the capability is marked verified.
        rows: Rows the probe came back with (None when it did not run).
    """

    domain: str
    source: str
    ok: bool
    error: str | None
    latency_ms: float
    verified: bool
    rows: int | None = None


def key_status(
    settings: Any | None = None,  # noqa: ANN401  # duck-typed settings for tests
) -> dict[str, dict[str, object]]:
    """Report which sources have their credentials configured.

    A source that requires a key but has none configured is not a
    health failure of the source - it is a deployment gap - so the
    patrol reports it separately from probe failures.

    Args:
        settings: Settings object; the application settings when
            omitted (tests inject a stub).

    Returns:
        ``{source: {"required": bool, "configured": bool, "endpoint": str}}``
    """
    from opendata.core.config import settings as app_settings

    settings = settings or app_settings

    keys = {
        "ths": {"present": bool(settings.fuyao_api_key), "required": True},
        "fred": {"present": bool(settings.fred_api_key), "required": True},
        "yfinance": {"present": True, "required": False},
        "ecb": {"present": True, "required": False},
        "imf": {"present": True, "required": False},
        "oecd": {"present": True, "required": False},
        "akshare": {"present": True, "required": False},
    }
    return {
        source: {
            "required": info["required"],
            "configured": info["present"],
            "endpoint": "fuyao.aicubes.cn"
            if source == "ths"
            else "api.stlouisfed.org"
            if source == "fred"
            else "n/a",
        }
        for source, info in keys.items()
    }


async def patrol(registry: ProviderRegistry | None = None) -> Sequence[PatrolResult]:
    """Probe every verified capability and refresh the registry health.

    Args:
        registry: Registry to probe; defaults to the process singleton.

    Returns:
        One result per registered capability, in registration order.
    """
    from opendata.data.registry import get_registry

    registry = registry or get_registry()
    results: list[PatrolResult] = []
    for capability in registry.capabilities():
        if not capability.verified:
            continue
        try:
            fetcher = registry.resolve(
                capability.asset_class, capability.domain, source=capability.source
            )
        except LookupError:
            logger.warning(f"patrol cannot resolve {capability.source}/{capability.domain}")
            continue
        started = time.perf_counter()
        try:
            rows = await asyncio.wait_for(
                _run_probe(fetcher, capability, registry), timeout=PROBE_TIMEOUT
            )
            ok, error = True, None
            registry.mark_available(fetcher)
        except PatrolProbeConfigError as exc:
            # A missing probe is a gap in the patrol, not an unhealthy
            # source: report it loudly but leave the routing mark alone.
            ok, error, rows = False, f"{type(exc).__name__}: {exc}", None
            logger.error(f"patrol cannot probe {capability.source}/{capability.domain}: {error}")
        except Exception as exc:  # any probe failure is a health signal
            ok, error, rows = False, f"{type(exc).__name__}: {exc}", None
            registry.mark_unavailable(fetcher)
            logger.warning(f"patrol failed for {capability.source}/{capability.domain}: {error}")
        results.append(
            PatrolResult(
                domain=capability.domain,
                source=capability.source,
                ok=ok,
                error=error,
                latency_ms=(time.perf_counter() - started) * 1000,
                verified=True,
                rows=rows,
            )
        )
    return results


def probe_params(
    capability: Capability,
    registry: ProviderRegistry,
    *,
    today: date | None = None,
) -> ProbeParams:
    """Resolve the minimal query one capability is probed with.

    Args:
        capability: The verified capability being probed.
        registry: Registry consulted by the rolling resolvers.
        today: Probe date for the rolling legs; defaults to today.

    Returns:
        Keyword arguments for the fetcher's ``fetch``.

    Raises:
        PatrolProbeConfigError: No probe is configured for the
            capability, or a rolling resolver found nothing to name.
    """
    key = (capability.domain, capability.source)
    resolver = PROBE_RESOLVERS.get(key)
    if resolver is not None:
        return resolver(today or date.today(), registry)
    if key in PROBE_PARAMS:
        return dict(PROBE_PARAMS[key])
    raise PatrolProbeConfigError(f"no probe params configured for {key[1]}/{key[0]}")


def rolling_futures_params(today: date, registry: ProviderRegistry | None = None) -> ProbeParams:
    """Name a listed futures contract from the probe date.

    Args:
        today: Probe date.
        registry: Unused; the rolling resolvers share one signature.

    Returns:
        Probe params for a contract month ``FUTURES_PROBE_LEAD_MONTHS``
        ahead, over a ``ROLLING_PROBE_WINDOW_DAYS`` trailing window.
    """
    del registry
    year, month = _months_ahead(today, FUTURES_PROBE_LEAD_MONTHS)
    return {
        "symbol": f"{FUTURES_PROBE_PRODUCT}{year % 100:02d}{month:02d}.SHF",
        "start_date": today - timedelta(days=ROLLING_PROBE_WINDOW_DAYS),
        "end_date": today,
    }


def rolling_option_params(today: date, registry: ProviderRegistry | None = None) -> ProbeParams:
    """Name a live option contract read from the instrument catalog.

    Option ``thscode`` is an opaque number, so the contract cannot be
    spelled from a date; the catalog is the only durable route.

    Args:
        today: Probe date.
        registry: Registry holding the catalog capability.

    Returns:
        Probe params for the longest-listed contract that is still
        tradable, over a ``ROLLING_PROBE_WINDOW_DAYS`` trailing window.

    Raises:
        PatrolProbeConfigError: The catalog capability is not registered.
        PatrolProbeError: No contract lives past the horizon.
    """
    if registry is None:
        from opendata.data.registry import get_registry

        registry = get_registry()
    catalog = _catalog_fetcher(registry)
    # ``fetch`` is typed against the union every capability can answer with;
    # the catalog capability answers with instrument rows.
    rows = cast("Sequence[Instrument]", catalog.fetch(asset_type="options"))
    return {
        "symbol": first_live_contract(rows, today),
        "start_date": today - timedelta(days=ROLLING_PROBE_WINDOW_DAYS),
        "end_date": today,
    }


def rolling_fund_etf_params(today: date, registry: ProviderRegistry | None = None) -> ProbeParams:
    """Probe the ETF daily leg over a window that rolls with the probe date.

    The instrument is fixed (``FUND_ETF_PROBE_SYMBOL`` is a large SSE-listed
    fund with a long trading history and distributions in most years), but the
    window cannot be: the fuyao channel answers a rolling ~5 years and refuses
    nothing else - asking past the floor yields an empty frame rather than an
    error, which the transport layer turns into a refusal. A frozen 2024
    window would therefore start failing years from now with the source in
    good health.

    Args:
        today: Probe date.
        registry: Unused; the rolling resolvers share one signature.

    Returns:
        Probe params over a ``ROLLING_PROBE_WINDOW_DAYS`` trailing window,
        which stays inside the rolling depth for as long as the fund trades.
    """
    del registry
    return {
        "symbol": FUND_ETF_PROBE_SYMBOL,
        "start_date": today - timedelta(days=ROLLING_PROBE_WINDOW_DAYS),
        "end_date": today,
    }


def first_live_contract(rows: Sequence[Instrument], today: date) -> str:
    """Pick the option symbol a probe can rely on surviving.

    Args:
        rows: Instrument catalog rows (``symbol``/``delist_date``).
        today: Probe date.

    Returns:
        The lowest symbol still listed ``OPTION_PROBE_HORIZON_DAYS`` after
        the probe date - the oldest contract of the listing, hence the one
        with the longest trading history behind it.

    Raises:
        PatrolProbeError: Every contract expires inside the horizon.
    """
    cutoff = today + timedelta(days=OPTION_PROBE_HORIZON_DAYS)
    live = sorted(
        (row.symbol for row in rows if row.delist_date is not None and row.delist_date >= cutoff),
    )
    if not live:
        raise PatrolProbeError(f"no option contract listed past {cutoff.isoformat()}")
    return live[0]


def _catalog_fetcher(registry: ProviderRegistry) -> Fetcher[Any, Any]:
    """Resolve the instrument capability the option probe reads.

    Args:
        registry: Registry to consult.

    Returns:
        The instrument catalog fetcher.

    Raises:
        PatrolProbeConfigError: The catalog leg is not registered.
    """
    domain, source = OPTION_PROBE_CATALOG
    capability = next(
        (cap for cap in registry.capabilities() if cap.domain == domain and cap.source == source),
        None,
    )
    if capability is None:
        raise PatrolProbeConfigError(f"option probe needs the {source}/{domain} catalog")
    return registry.resolve(capability.asset_class, domain, source=source)


def _months_ahead(today: date, months: int) -> tuple[int, int]:
    """Shift a date forward by whole months.

    Args:
        today: Base date.
        months: Months to add.

    Returns:
        ``(year, month)`` of the target month.
    """
    index = today.year * 12 + (today.month - 1) + months
    return index // 12, index % 12 + 1


async def _run_probe(
    fetcher: Fetcher[Any, Any], capability: Capability, registry: ProviderRegistry
) -> int:
    """Assemble and run one probe, off the event loop.

    The rolling resolvers read the instrument catalog over the network,
    so param assembly belongs inside the timeout with the fetch.

    Args:
        fetcher: The resolved fetcher.
        capability: The verified capability being probed.
        registry: Registry the resolvers consult.

    Returns:
        The number of rows the probe came back with.

    Raises:
        PatrolProbeError: The probe is not configured, could not name an
            instrument, or came back empty.
    """
    params = await asyncio.to_thread(probe_params, capability, registry)
    return await _probe_fetcher(fetcher, params)


async def _probe_fetcher(fetcher: Fetcher[Any, Any], params: ProbeParams) -> int:
    """Run one fetcher with its minimal probe params.

    An empty answer counts as a failure: every probe below names an
    instrument that published in its window, so zero rows means the
    source stopped answering rather than that it has nothing to say.

    Args:
        fetcher: The resolved fetcher.
        params: Probe params for this capability.

    Returns:
        The number of rows the probe came back with.

    Raises:
        PatrolProbeError: The fetch succeeded but returned nothing.
        Exception: The fetch itself failed (the patrol records it).
    """
    result = await asyncio.to_thread(fetcher.fetch, **params)  # type: ignore[arg-type]  # dynamic probe kwargs
    rows = len(result) if result is not None else 0
    if rows == 0:
        raise PatrolProbeError("probe returned no rows")
    return rows


#: Legs whose probe params cannot be frozen. ``futures_daily`` /
#: ``option_daily`` name an instrument that expires: contract months roll
#: off, so a static probe there rots exactly like the mis-configured ones it
#: replaces. ``fund_etf_daily`` on ths names a permanent fund but cannot
#: freeze its *window*: that channel answers a rolling 1827 days (measured,
#: ``docs/evidence/C20`` criterion D), so a fixed historical range silently
#: walks out of coverage one day and the probe then fails on a source that
#: is answering perfectly.
PROBE_RESOLVERS: dict[tuple[str, str], Callable[[date, ProviderRegistry | None], ProbeParams]] = {
    ("futures_daily", "ths"): rolling_futures_params,
    ("option_daily", "ths"): rolling_option_params,
    ("fund_etf_daily", "ths"): rolling_fund_etf_params,
}


def run_patrol() -> Sequence[PatrolResult]:
    """Sync entry point for a cron/script invocation.

    Returns:
        The probe results.
    """
    return asyncio.run(patrol())
