"""Source health patrol (B3.4 / AC-4 "P0 域接口每日健康巡检").

An *active* counterpart to the registry's passive health marks: probes
each registered verified capability with a tiny real query, updates the
registry's health so auto routing skips a broken source, and reports
the outcome together with the key configuration of every source.

The probe parameters are per-domain and deliberately minimal - one
symbol or one series - because a patrol that must assemble a full
query would be as fragile as the sources it checks.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

from loguru import logger

if TYPE_CHECKING:
    from collections.abc import Sequence
    from typing import Any

    from opendata.data.protocol import Fetcher
    from opendata.data.registry import ProviderRegistry

#: Per-second timeout of one probe (a healthy source answers fast).
PROBE_TIMEOUT = 10.0
#: Domains whose probe hits a broad market endpoint; None skips the
#: symbol filter for domains that take a series id instead.
PROBE_PARAMS: dict[str, dict[str, str]] = {
    "stock_daily": {"symbol": "600519"},
    "stock_daily_overseas": {"symbol": "AAPL"},
    "economy_cpi": {"series_id": "CPIAUCSL"},
    "economy_gdp": {"series_id": "GDP"},
    "economy_rate": {"series_id": "IRSTCI01USM156N"},
    "economy_unemployment": {"series_id": "UNRATE"},
    "stock_action": {"symbol": "600519"},
    "stock_adjust": {"symbol": "600519"},
}


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
    """

    domain: str
    source: str
    ok: bool
    error: str | None
    latency_ms: float
    verified: bool


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
            await asyncio.wait_for(
                _probe_fetcher(fetcher, capability.domain), timeout=PROBE_TIMEOUT
            )
            ok, error = True, None
            registry.mark_available(fetcher)
        except Exception as exc:  # any probe failure is a health signal
            ok, error = False, f"{type(exc).__name__}: {exc}"
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
            )
        )
    return results


async def _probe_fetcher(fetcher: Fetcher[Any, Any], domain: str) -> None:
    """Run one fetcher with its minimal probe params.

    Args:
        fetcher: The resolved fetcher.
        domain: Domain identifier (selects the probe params).

    Raises:
        Exception: The fetch itself failed (the patrol records it).
    """
    params = PROBE_PARAMS.get(domain, {})
    await asyncio.to_thread(fetcher.fetch, **params)  # type: ignore[arg-type]  # dynamic probe kwargs


def run_patrol() -> Sequence[PatrolResult]:
    """Sync entry point for a cron/script invocation.

    Returns:
        The probe results.
    """
    return asyncio.run(patrol())
