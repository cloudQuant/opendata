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

One blip is retried before a source is marked unhealthy, because that
mark holds until the next patrol. Retrying is not a way to hide
flakiness: a leg that only passes on its second attempt is reported as
flaky and keeps the failure the first attempt produced.

Row counts alone cannot see a column hollowing out inside a full page,
which is how C18 caught the a-share catalog answering 5,578 rows with no
``list_date`` at all while the patrol printed ``[ok]``. Legs listed in
:data:`FIELD_CANARIES` therefore get their watched columns measured after a
pass, and only shapes that were measured on the live source count as known
- a deviation pages, but never moves the routing mark, because one column
disagreeing with itself between two catalog builds is not "this source is
down".
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from datetime import date, timedelta
from typing import TYPE_CHECKING, TypedDict, cast

from loguru import logger

from opendata.pipeline.key_health import (
    CLASS_PATROL_GAP,
    FailureObservation,
    KeyReport,
    attribution_of,
    build_report,
    classify_failure,
    redact,
)

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
#: Attempts one leg gets before its source is judged unhealthy. A single
#: blip - a dropped connection, one 429 - is not an outage, and marking the
#: source unhealthy takes it out of ``auto`` routing until the next patrol.
PROBE_ATTEMPTS = 2
#: Seconds between a failed attempt and its retry.
PROBE_RETRY_BACKOFF = 2.0
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


class KeyStatus(TypedDict):
    """Public, non-secret metadata for one configured provider key."""

    required: bool
    configured: bool
    endpoint: str
    expires_at: date | None


PROBE_PARAMS: dict[tuple[str, str], ProbeParams] = {
    ("economy_cpi", "ecb"): {
        "series_id": "ICP/M.U2.N.000000.4.ANR",
        "start_date": date(2024, 1, 1),
        "end_date": date(2024, 12, 31),
    },
    ("economy_cpi", "fred"): {
        "series_id": "CPIAUCSL",
        "start_date": date(2020, 1, 1),
        "end_date": date(2026, 8, 1),
    },
    ("economy_gdp", "ecb"): {
        "series_id": "MNA/Q.N.I9.W2.S1.S1.B.B1GQ._Z._Z._Z.EUR.LR.N",
        "start_date": date(2023, 1, 1),
        "end_date": date(2024, 12, 31),
    },
    ("economy_gdp", "fred"): {
        "series_id": "GDPC1",
        "start_date": date(2020, 1, 1),
        "end_date": date(2026, 8, 1),
    },
    ("economy_rate", "ecb"): {
        "series_id": "FM/B.U2.EUR.4F.KR.MRR_FR.LEV",
        "start_date": date(2022, 1, 1),
        "end_date": date(2024, 12, 31),
    },
    ("economy_unemployment", "fred"): {
        "series_id": "UNRATE",
        "start_date": date(2020, 1, 1),
        "end_date": date(2026, 8, 1),
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
        error: Failure detail (None on a clean pass; the first attempt's
            failure when a flaky leg only passed on its retry).
        latency_ms: Probe duration in milliseconds.
        verified: Whether the capability is marked verified.
        rows: Rows the probe came back with (None when it did not run).
        attempts: Probe attempts used up (1 = passed first try, or failed
            them all).
        canaries: Column-shape readings taken after a clean pass, empty for a
            leg with no watched columns or no pass to look at.
        failure_class: Health class of the failure the report keeps, from
            :func:`~opendata.pipeline.key_health.classify_failure` (None when
            nothing failed).
        attribution: Metadata-only description of that failure - code, status,
            URL origin (None when there was no failure to describe).
    """

    domain: str
    source: str
    ok: bool
    error: str | None
    latency_ms: float
    verified: bool
    rows: int | None = None
    attempts: int = 1
    canaries: tuple[CanaryReading, ...] = ()
    failure_class: str | None = None
    attribution: str | None = None

    @property
    def flaky(self) -> bool:
        """Whether this leg only passed because it was retried.

        Returns:
            True when a retry produced the pass. A flaky leg keeps the
            routing mark of a healthy source but is reported separately:
            absorbing a blip must not turn flakiness green.
        """
        return self.ok and self.attempts > 1

    @property
    def field_deviations(self) -> tuple[CanaryReading, ...]:
        """Canary readings whose column shape was never measured on this source.

        Returns:
            The deviating readings (empty when every watched column kept a
            known shape). Independent of ``ok``: a leg can probe clean and
            still be hollowing out a column.
        """
        return tuple(reading for reading in self.canaries if reading.deviates)


#: A watched column is filled on every row (within :data:`CANARY_MISSING_TOLERANCE`).
SHAPE_FULL = "full"
#: A watched column is filled on no row at all.
SHAPE_HOLLOW = "hollow"
#: A watched column is filled on some but not all rows beyond tolerance.
SHAPE_PARTIAL = "partial"
#: The canary could not be measured this run - not a statement about the data.
SHAPE_NO_READING = "no-reading"
#: Rows a column may miss and still count as ``full``. Inherited from the
#: criterion C14 accepted for the a-share catalog (9 of 5,578 rows missing
#: ``list_date``): a handful of records with no listing date is a normal
#: catalog, while a column that lost its content did not lose five rows.
CANARY_MISSING_TOLERANCE = 20


@dataclass(frozen=True)
class FieldCanary:
    """One column of one catalog page the patrol watches for hollowing out.

    Attributes:
        asset_type: Catalog page the canary reads.
        field: Contract attribute watched on that page.
        allowed: Shapes measured on the live source. A shape outside this set
            is news, which is what makes the reading an alarm rather than a
            re-report of a known upstream gap.
        reason: Where the allowed set was measured, so a reviewer can check
            the reading instead of trusting this table.
    """

    asset_type: str
    field: str
    allowed: frozenset[str]
    reason: str


@dataclass(frozen=True)
class CanaryReading:
    """Result of measuring one watched column once.

    Attributes:
        asset_type: Catalog page that was read.
        field: Contract attribute that was measured.
        shape: One of the ``SHAPE_*`` values.
        rows: Rows the read came back with (None when it did not run).
        missing: Rows whose watched column is empty.
        allowed: Shapes the patrol considers known for this column.
        error: Why nothing could be measured (None on a real reading).
    """

    asset_type: str
    field: str
    shape: str
    rows: int | None
    missing: int | None
    allowed: frozenset[str]
    error: str | None = None

    @property
    def deviates(self) -> bool:
        """Whether this reading should page.

        Returns:
            True when a real reading produced a shape never measured on this
            column. An unmeasurable reading is not a deviation: "could not
            look" is not "the data is wrong", and reporting it as one would
            turn every network blip into a false field-collapse alarm.
        """
        return self.shape != SHAPE_NO_READING and self.shape not in self.allowed


#: Columns the row-count probe cannot see, keyed by ``(domain, source)``.
#:
#: C18's canary found the a-share catalog's ``list_date`` empty in a snapshot
#: that the patrol had been printing as ``[ok] ths/instrument: 5578 rows`` all
#: along - counting rows is blind to a column being hollow inside a full page.
#:
#: The allowed shapes below are only what has actually been measured on the
#: live source: C19's 36-read flip-rate run
#: (``docs/evidence/C19/catalog-flip-rate-all-types.txt``) and C25's
#: 10-read × 4-page × 8-column run (``docs/evidence/C25/field-canary-measure.txt``),
#: which agree on every column. Where the two catalog builds differ *in* a
#: column (a-share ``list_date``: 满值 14 次 / 全空 22 次) both shapes are
#: allowed, because an alarm there would fire on whichever build answered
#: rather than on the data - a shape neither build produces is still news.
#:
#: ``board`` is watched by no one on purpose: it is hollow on all four pages
#: today, so a future backfill would page as a deviation while being an
#: improvement.
FIELD_CANARIES: dict[tuple[str, str], tuple[FieldCanary, ...]] = {
    ("instrument", "ths"): (
        FieldCanary(
            asset_type="a-share",
            field="list_date",
            allowed=frozenset({SHAPE_FULL, SHAPE_HOLLOW}),
            reason="C19 36 reads: 满值 5570/5578 ×14 / 全空 ×22，两份目录构建之差",
        ),
        FieldCanary(
            asset_type="a-share",
            field="name",
            allowed=frozenset({SHAPE_FULL}),
            reason="C25 10 reads: 5578/5578 有名，这是目录可 join 的载荷本身",
        ),
        FieldCanary(
            asset_type="a-share-index",
            field="list_date",
            allowed=frozenset({SHAPE_FULL}),
            reason="C19 24 + C25 10 reads: 1431/1431 每次都有日期",
        ),
        FieldCanary(
            asset_type="futures",
            field="list_date",
            allowed=frozenset({SHAPE_PARTIAL}),
            reason="C19 24 + C25 10 reads: 恒 1142 行缺 265（7777/8888/9999 合成系列）",
        ),
    ),
}


def shape_of(rows: int, missing: int, *, tolerance: int = CANARY_MISSING_TOLERANCE) -> str:
    """Classify how much of a watched column one read filled.

    Hollow is tested before the tolerance, not after it: a small page whose
    every row is empty is a hollow column, while a tolerance checked first
    would read any page of twenty rows or fewer as full.

    Args:
        rows: Rows read.
        missing: Rows whose watched column is empty.
        tolerance: Misses that still count as a full column.

    Returns:
        ``SHAPE_FULL`` / ``SHAPE_PARTIAL`` / ``SHAPE_HOLLOW``.

    Raises:
        ValueError: Nothing to measure (zero rows), or ``missing`` exceeds
            ``rows``, which would mean the reading was assembled wrong rather
            than the source being wrong.
    """
    if rows <= 0:
        raise ValueError("cannot classify a column over 0 rows")
    if missing > rows:
        raise ValueError(f"cannot miss {missing} of {rows} rows")
    if missing >= rows:
        return SHAPE_HOLLOW
    return SHAPE_FULL if missing <= tolerance else SHAPE_PARTIAL


def _is_missing(value: object) -> bool:
    """Whether one watched cell counts as empty.

    ``None`` and a blank string are the two ways this catalog publishes "no
    value"; a date field only ever has the first.

    Args:
        value: The attribute read off a contract row.

    Returns:
        True when the cell carries no information.
    """
    return value is None or (isinstance(value, str) and not value.strip())


def evaluate_canary(canary: FieldCanary, rows: Sequence[object]) -> CanaryReading:
    """Measure one watched column over the rows a catalog read came back with.

    Args:
        canary: The column being watched and the shapes it may take.
        rows: Contract rows from the read.

    Returns:
        The reading. An empty page is reported as unmeasurable rather than
        ``hollow``: zero rows says the page is gone, which the row-count probe
        already owns, and a shape computed over nothing would be a claim about
        a column that was never looked at.
    """
    if not rows:
        return CanaryReading(
            asset_type=canary.asset_type,
            field=canary.field,
            shape=SHAPE_NO_READING,
            rows=0,
            missing=None,
            allowed=canary.allowed,
            error="canary read returned no rows",
        )
    missing = sum(1 for row in rows if _is_missing(getattr(row, canary.field)))
    return CanaryReading(
        asset_type=canary.asset_type,
        field=canary.field,
        shape=shape_of(len(rows), missing),
        rows=len(rows),
        missing=missing,
        allowed=canary.allowed,
    )


def key_status(
    settings: Any | None = None,  # noqa: ANN401  # duck-typed settings for tests
) -> dict[str, KeyStatus]:
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

    keys: dict[str, tuple[bool, bool, date | None]] = {
        "ths": (
            bool(settings.fuyao_api_key),
            True,
            _configured_expiry(getattr(settings, "fuyao_api_key_expires_at", None)),
        ),
        "fred": (
            bool(settings.fred_api_key),
            True,
            _configured_expiry(getattr(settings, "fred_api_key_expires_at", None)),
        ),
        "yfinance": (True, False, None),
        "ecb": (True, False, None),
        "imf": (True, False, None),
        "oecd": (True, False, None),
        "akshare": (True, False, None),
    }
    status: dict[str, KeyStatus] = {}
    for source, (configured, required, expires_at) in keys.items():
        status[source] = {
            "required": required,
            "configured": configured,
            "endpoint": (
                "fuyao.aicubes.cn"
                if source == "ths"
                else "api.stlouisfed.org"
                if source == "fred"
                else "n/a"
            ),
            "expires_at": expires_at,
        }
    return status


def _configured_expiry(value: object) -> date | None:
    """Return only a validated date from optional issuer metadata."""
    return value if isinstance(value, date) else None


def key_values(settings: Any | None = None) -> tuple[str, ...]:  # noqa: ANN401
    """Return the Key values this process holds, for report scrubbing only.

    Args:
        settings: Settings object; the application settings when omitted.

    Returns:
        The configured credential values. Nothing here prints, stores or
        returns them to a caller of :func:`patrol`: they are used as the
        replace-pattern of :func:`~opendata.pipeline.key_health.redact`, which
        is the only guard against a client that quotes its own header into a
        failure message - a shape no URL rule can catch.
    """
    from opendata.core.config import settings as app_settings

    settings = settings or app_settings
    return tuple(
        value
        for value in (
            getattr(settings, "fuyao_api_key", ""),
            getattr(settings, "fred_api_key", ""),
        )
        if value
    )


def credential_health(
    settings: Any | None = None,  # noqa: ANN401  # duck-typed settings for tests
    results: Sequence[PatrolResult] = (),
    *,
    recent_observations: Sequence[FailureObservation] = (),
    as_of: date | None = None,
) -> dict[str, KeyReport]:
    """Grade each source's credential plane (AC-19 / NFR-5).

    Presence is what :func:`key_status` can see; the classes come from the
    probes that actually ran. Joining them here, in one function, is what keeps
    a report line and an API response from disagreeing about the same Key.

    Args:
        settings: Settings object; the application settings when omitted.
        results: Probe results of one patrol. A leg that passed on its retry
            still contributes its first failure, so a 429 the retry absorbed is
            reported as quota consumption rather than hidden.
        recent_observations: Sanitized failures from the process-local passive
            health store.
        as_of: Reference date for issuer-supplied expiry metadata.

    Returns:
        ``{source: KeyReport}``, metadata only - no Key value, no header, no URL
        query. Levels are ``alert`` / ``warn`` / ``info`` / ``presence-only`` /
        ``not-applicable``; a configured Key that was never seen to fail is
        ``presence-only``, never healthy. Call ``as_dict()`` to render one.
    """
    if settings is None:
        from opendata.core.config import settings as app_settings

        settings = app_settings
    statuses = key_status(settings)
    observed_results = tuple(
        FailureObservation(result.source, str(result.failure_class), result.attribution)
        for result in results
        if result.failure_class is not None
    )
    observations = (*observed_results, *recent_observations)
    return {
        source: build_report(
            source,
            required=bool(info["required"]),
            configured=bool(info["configured"]),
            endpoint=str(info["endpoint"]),
            observations=observations,
            expires_at=(
                info.get("expires_at") if isinstance(info.get("expires_at"), date) else None
            ),
            as_of=as_of,
            expiry_warning_days=int(getattr(settings, "key_expiry_warning_days", 14)),
        )
        for source, info in statuses.items()
    }


async def patrol(
    registry: ProviderRegistry | None = None,
    *,
    capabilities: Sequence[Capability] | None = None,
) -> Sequence[PatrolResult]:
    """Probe selected verified capabilities and refresh registry health.

    Args:
        registry: Registry to probe; defaults to the process singleton.
        capabilities: Optional selected legs to probe. The full registry
            remains available to rolling probe resolvers for metadata
            dependencies. None preserves the historical all-capabilities
            behavior.

    Returns:
        One result per selected registered verified capability, in input
        order, each naming the attempts it used up and the column-shape
        readings taken after its pass. With no selection, every verified
        registry capability is probed as before.
    """
    from opendata.data.registry import get_registry

    registry = registry or get_registry()
    secrets = key_values()
    results: list[PatrolResult] = []
    selected_capabilities = registry.capabilities() if capabilities is None else capabilities
    for capability in selected_capabilities:
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
        leg = f"{capability.source}/{capability.domain}"
        attempts = 0
        ok = False
        rows: int | None = None
        error: str | None = None
        failure_class: str | None = None
        attribution: str | None = None
        while True:
            attempts += 1
            try:
                rows = await asyncio.wait_for(
                    _run_probe(fetcher, capability, registry), timeout=PROBE_TIMEOUT
                )
                ok = True
                registry.mark_available(fetcher)
                break
            except PatrolProbeConfigError as exc:
                # A missing probe is a gap in the patrol, not an unhealthy
                # source: report it loudly, leave the routing mark alone, and
                # never retry - a second attempt reads the same table.
                error = redact(f"{type(exc).__name__}: {exc}", secrets)
                failure_class, attribution = CLASS_PATROL_GAP, None
                logger.error(f"patrol cannot probe {leg}: {error}")
                break
            except Exception as exc:  # any probe failure is a health signal
                rows, error = None, redact(f"{type(exc).__name__}: {exc}", secrets)
                failure_class, attribution = classify_failure(exc), attribution_of(exc)
                if attempts < PROBE_ATTEMPTS:
                    logger.warning(f"patrol retrying {leg} after attempt {attempts}: {error}")
                    await asyncio.sleep(PROBE_RETRY_BACKOFF)
                    continue
                registry.mark_unavailable(fetcher)
                logger.warning(f"patrol failed for {leg} on {attempts} attempts: {error}")
                break
        if ok and attempts > 1:
            # The pass is real, the leg is not clean: keep the first failure
            # in the report rather than turning flakiness green.
            logger.warning(f"patrol {leg} passed only on attempt {attempts}: {error}")
        probe_ms = (time.perf_counter() - started) * 1000
        canaries = await _run_canaries(fetcher, capability) if ok else ()
        for reading in canaries:
            if reading.deviates:
                logger.warning(
                    f"patrol canary {leg} {reading.asset_type}.{reading.field}: "
                    f"{reading.missing} of {reading.rows} rows empty, shape "
                    f"{reading.shape} (measured: {sorted(reading.allowed)})"
                )
        results.append(
            PatrolResult(
                domain=capability.domain,
                source=capability.source,
                ok=ok,
                error=error,
                latency_ms=probe_ms,
                verified=True,
                rows=rows,
                attempts=attempts,
                canaries=canaries,
                failure_class=failure_class,
                attribution=attribution,
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


async def _run_canaries(
    fetcher: Fetcher[Any, Any], capability: Capability
) -> tuple[CanaryReading, ...]:
    """Measure the watched columns of a leg that just probed clean.

    One catalog page is read once however many of its columns are watched:
    the shapes must come from the *same* answer, or a column could be judged
    full on the build that has dates and hollow on the one that does not and
    both readings would be true of nothing.

    A canary read that fails is reported as unmeasurable and nothing else:
    the routing mark was decided by the probe, and one page that could not be
    read today must not be allowed to say anything about the source's health
    or to page as a field collapse.

    Args:
        fetcher: The resolved fetcher the leg probes with.
        capability: The capability that passed its probe.

    Returns:
        One reading per watched column, in table order.
    """
    watched = FIELD_CANARIES.get((capability.domain, capability.source), ())
    readings: list[CanaryReading] = []
    for asset_type in dict.fromkeys(canary.asset_type for canary in watched):
        rows, error = await _read_page(fetcher, asset_type)
        on_page = (canary for canary in watched if canary.asset_type == asset_type)
        readings.extend(_page_reading(canary, rows, error) for canary in on_page)
    return tuple(readings)


def _page_reading(
    canary: FieldCanary, rows: Sequence[object] | None, error: str | None
) -> CanaryReading:
    """Render one column's reading from a page that may not have arrived.

    Args:
        canary: The column being watched and the shapes it may take.
        rows: The page's rows, or None when the page could not be read.
        error: Why the page could not be read (None on a real read).

    Returns:
        The measurement, or an unmeasurable reading.
    """
    if rows is None:
        return CanaryReading(
            asset_type=canary.asset_type,
            field=canary.field,
            shape=SHAPE_NO_READING,
            rows=None,
            missing=None,
            allowed=canary.allowed,
            error=error,
        )
    return evaluate_canary(canary, rows)


async def _read_page(
    fetcher: Fetcher[Any, Any], asset_type: str
) -> tuple[Sequence[object] | None, str | None]:
    """Read one catalog page for the canaries, or explain why it could not.

    Args:
        fetcher: The fetcher serving the page.
        asset_type: The catalog page to read.

    Returns:
        ``(rows, None)`` on a read, ``(None, detail)`` when nothing could be
        measured - a timeout, a refusal, or a transport blip alike.
    """
    try:
        rows = await asyncio.wait_for(
            asyncio.to_thread(fetcher.fetch, asset_type=asset_type),
            timeout=PROBE_TIMEOUT,
        )
    except Exception as exc:  # an unreadable page measures nothing
        return None, f"{type(exc).__name__}: {exc}"
    return cast("Sequence[object]", rows), None


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
