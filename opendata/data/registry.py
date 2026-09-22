"""Provider registry and source routing (design §4.3 / §4.4, FR-3).

The registry is a process-wide singleton that records fetchers with
their capability, tracks health, and routes ``resolve()`` calls:
explicit sources route directly (fail closed, no fallback), while
``source="auto"`` orders candidates by the authority baseline and
returns the first available one.

The authority baseline lives in ``authority.json`` next to this
module: source names there are routing labels, not code references,
which keeps the runtime package clean of upstream coupling (AC-16
zero-dependency assertion, scan surface = ``*.py`` of runtime
packages).
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from opendata.data.capability import Capability
    from opendata.data.protocol import Fetcher

_AUTHORITY_PATH = Path(__file__).parent / "authority.json"


@lru_cache(maxsize=1)
def authority_baseline() -> dict[str, tuple[str, ...]]:
    """Load the design §4.4 authority baseline from ``authority.json``.

    Domains not listed there (macro / overseas) declare authority per
    provider at registration time; until then they route in
    registration order.

    Returns:
        Domain identifier to ordered source names (authority first).

    Raises:
        RuntimeError: If the file is missing or malformed (fail
            closed, quality spec §0).
    """
    try:
        data = json.loads(_AUTHORITY_PATH.read_text(encoding="utf-8"))
        raw = data["domains"]
    except (OSError, json.JSONDecodeError, KeyError, TypeError) as exc:
        raise RuntimeError(f"authority baseline {_AUTHORITY_PATH} is unreadable: {exc}") from exc
    baseline: dict[str, tuple[str, ...]] = {}
    for domain, sources in raw.items():
        if not isinstance(domain, str) or not isinstance(sources, list):
            raise RuntimeError(f"malformed authority entry for {domain!r}")
        if not all(isinstance(source, str) for source in sources):
            raise RuntimeError(f"malformed authority sources for {domain!r}")
        baseline[domain] = tuple(sources)
    return baseline


def _capability_key(capability: Capability) -> str:
    """Build the stable registry key of a capability.

    Args:
        capability: The capability to key.

    Returns:
        An ``asset_class:domain:period:market:source`` string.
    """
    return (
        f"{capability.asset_class}:{capability.domain}:"
        f"{capability.period}:{capability.market}:{capability.source}"
    )


class ProviderRegistry:
    """Process-wide registry of data source fetchers (design §4.3).

    Registers fetchers keyed by their capability, tracks health per
    key, and resolves routing requests. Duplicate capability keys are
    rejected so one capability always has exactly one fetcher.

    The registry stores heterogeneous fetchers, hence the ``Any``
    type parameters: the protocol types are invariant, and callers
    receive a fetcher whose ``fetch()`` result is typed at the call
    site by the domain-specific facade, not by the registry.
    """

    def __init__(self) -> None:
        """Initialize an empty registry with all sources healthy."""
        self._fetchers: dict[str, Fetcher[Any, Any]] = {}
        self._registration_order: dict[str, int] = {}
        self._healthy: dict[str, bool] = {}

    def register(self, fetcher: Fetcher[Any, Any]) -> None:
        """Register a fetcher under its declared capability.

        Args:
            fetcher: The fetcher instance; its capability must be set.

        Raises:
            ValueError: If the capability key is already registered.
        """
        key = _capability_key(fetcher.capability)
        if key in self._fetchers:
            raise ValueError(f"capability already registered: {key}")
        self._fetchers[key] = fetcher
        self._registration_order[key] = len(self._registration_order)
        self._healthy.pop(key, None)

    def capabilities(self) -> list[Capability]:
        """List the capabilities of all registered fetchers."""
        return [fetcher.capability for fetcher in self._fetchers.values()]

    def mark_unavailable(self, fetcher: Fetcher[Any, Any]) -> None:
        """Mark a fetcher unhealthy so auto routing skips it.

        Args:
            fetcher: The fetcher that failed (e.g. transport error).
        """
        self._healthy[_capability_key(fetcher.capability)] = False

    def mark_available(self, fetcher: Fetcher[Any, Any]) -> None:
        """Mark a fetcher healthy again after a successful call.

        Args:
            fetcher: The fetcher that recovered.
        """
        self._healthy[_capability_key(fetcher.capability)] = True

    def resolve(
        self,
        asset_class: str,
        domain: str,
        *,
        period: str | None = None,
        market: str | None = None,
        source: str = "auto",
    ) -> Fetcher[Any, Any]:
        """Route a request to a fetcher (design §4.3).

        With ``source="auto"`` the candidates matching the requested
        capability fields are ordered by the authority baseline
        (unlisted sources follow in registration order) and the first
        *healthy* one is returned; unverified or on-demand /
        upstream-pending capabilities never participate. With an
        explicit source the matching fetcher is returned as-is (its
        verification state is the caller's responsibility) and a miss
        fails closed - no fallback.

        Args:
            asset_class: e.g. ``equity`` / ``futures``.
            domain: Domain identifier, e.g. ``stock_daily``.
            period: Optional period filter (e.g. ``1D``).
            market: Optional market filter (e.g. ``cn``).
            source: ``"auto"`` or an explicit source name.

        Returns:
            The resolved fetcher.

        Raises:
            LookupError: If no capability matches, the explicit source
                is unknown, or no auto candidate is healthy.
        """
        candidates = [
            (key, fetcher)
            for key, fetcher in self._fetchers.items()
            if self._matches(fetcher.capability, asset_class, domain, period, market)
        ]
        if source != "auto":
            explicit = [
                (key, fetcher) for key, fetcher in candidates if fetcher.capability.source == source
            ]
            if not explicit:
                raise LookupError(
                    f"source {source!r} has no registered capability for "
                    f"{asset_class}/{domain} (period={period}, market={market})"
                )
            return explicit[0][1]

        ranked = sorted(
            [
                (key, fetcher)
                for key, fetcher in candidates
                if fetcher.capability.participates_in_auto()
            ],
            key=lambda pair: self._authority_rank(pair[0], domain),
        )
        for key, fetcher in ranked:
            if self._healthy.get(key, True):
                return fetcher
        if not ranked:
            raise LookupError(
                f"no verified capability registered for {asset_class}/{domain} "
                f"(period={period}, market={market})"
            )
        raise LookupError(f"all auto candidates for {asset_class}/{domain} are unavailable")

    def resolve_domain(self, domain: str, *, source: str = "auto") -> Fetcher[Any, Any]:
        """Route by domain identifier alone (catalog-facing, FR-17).

        Domains are unique across asset classes (domains.yaml), so
        matching by domain is unambiguous: the first capability found
        supplies the asset class for the full routing rules.

        Args:
            domain: Domain identifier, e.g. ``stock_daily``.
            source: ``"auto"`` or an explicit source name.

        Returns:
            The resolved fetcher.

        Raises:
            LookupError: If no capability is registered for the domain.
        """
        for fetcher in self._fetchers.values():
            if fetcher.capability.domain == domain:
                return self.resolve(fetcher.capability.asset_class, domain, source=source)
        raise LookupError(f"no capability registered for domain {domain!r}")

    def _matches(
        self,
        capability: Capability,
        asset_class: str,
        domain: str,
        period: str | None,
        market: str | None,
    ) -> bool:
        """Check a capability against the requested routing fields."""
        if capability.asset_class != asset_class or capability.domain != domain:
            return False
        if period is not None and capability.period != period:
            return False
        return market is None or capability.market == market

    def _authority_rank(self, key: str, domain: str) -> tuple[int, int]:
        """Rank a candidate for auto routing within a domain.

        Sources listed in the authority baseline come first in their
        declared order; unlisted sources follow in registration order.

        Args:
            key: The capability key of the candidate.
            domain: The requested domain (identifies the baseline row).

        Returns:
            An ``(authority_index, registration_index)`` sort key.
        """
        source = key.rsplit(":", 1)[1]
        order = authority_baseline().get(domain, ())
        authority = order.index(source) if source in order else len(order)
        return authority, self._registration_order.get(key, 0)


@lru_cache(maxsize=1)
def get_registry() -> ProviderRegistry:
    """Return the process-wide provider registry singleton."""
    return ProviderRegistry()
