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
import keyword
import logging
import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping, Sequence

    from opendata.data.capability import Capability
    from opendata.data.protocol import Fetcher

_AUTHORITY_PATH = Path(__file__).parent / "authority.json"
_CREDENTIAL_REQUIREMENTS = {
    "ths": ("FUYAO_API_KEY", "fuyao_api_key"),
    "fred": ("FRED_API_KEY", "fred_api_key"),
}
logger = logging.getLogger(__name__)


def _credential_environment_name(source: str) -> str | None:
    """Return the environment variable that enables a credentialed source."""
    requirement = _CREDENTIAL_REQUIREMENTS.get(source.lower())
    return requirement[0] if requirement is not None else None


def _source_has_credentials(source: str) -> bool:
    """Check the same environment-then-settings key sources as provider clients."""
    requirement = _CREDENTIAL_REQUIREMENTS.get(source.lower())
    if requirement is None:
        return True
    environment_name, settings_name = requirement
    value = os.environ.get(environment_name)
    if value and value.strip():
        return True
    from opendata.core.config import get_settings

    settings_value = getattr(get_settings(), settings_name, None)
    return isinstance(settings_value, str) and bool(settings_value.strip())


@lru_cache(maxsize=1)
def authority_baseline() -> dict[str, tuple[str, ...]]:
    """Load the design §4.4 authority baseline from ``authority.json``.

    Every routing-relevant domain is listed: a domain missing there would
    rank its candidates by registration order, which makes the auto winner
    an artifact of file collection order (the macro domains were exactly
    that case until C22). ``authority.json``'s ``_comment`` records the
    basis for the macro rows.

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


def reconcile_authority(
    capabilities: Iterable[Capability],
    *,
    baseline: Mapping[str, Sequence[str]] | None = None,
) -> tuple[str, ...]:
    """Cross-check the declared authority table against what is registered.

    Every row of ``authority.json`` is a claim of the shape "source S is a
    candidate for domain D, at this rank", and nothing on the routing path
    verifies those claims. Two failure modes were measured (C23): a claim
    outliving the leg it names - seven pairs were listed for capabilities
    that were never registered, and ``GET /api/v1/data/sources`` returned
    them as if they were routable - and a registered leg the table never
    ranks, which falls back to registration order (the C22 macro defect).
    Checking both directions at once is what keeps the table a description
    of the registry rather than a parallel document.

    This is a whole-deployment check, not a per-call invariant: a process
    that registers one provider under test legitimately serves a slice of
    the table, so ``resolve()`` must stay agnostic and the reconciliation
    runs against the fully registered registry (the gate) instead.

    A domain served by a single source needs no row: there is nothing to
    order. Demanding one anyway would put the table in the position of
    certifying a ranking it cannot express, which is the reason earlier
    rounds deliberately left ``fund_action`` and the overseas daily leg
    out (AC-10: "登记成权威序属口径造假").

    Args:
        capabilities: Registered capabilities to check against, normally
            ``get_registry().capabilities()``.
        baseline: The declared table; defaults to :func:`authority_baseline`.

    Returns:
        Violation messages prefixed with the rule each one breaks, sorted,
        and empty when the table and the registry describe the same legs.
    """
    declared = authority_baseline() if baseline is None else baseline
    served: dict[str, set[str]] = {}
    auto_eligible: dict[str, set[str]] = {}
    for capability in capabilities:
        served.setdefault(capability.domain, set()).add(capability.source)
        if capability.participates_in_auto():
            auto_eligible.setdefault(capability.domain, set()).add(capability.source)

    violations: list[str] = []
    for domain, row in declared.items():
        if not row:
            violations.append(f"empty-row: {domain} is listed with no source")
            continue
        registered_for_row = served.get(domain, set())
        violations.extend(
            f"phantom-leg: {domain}/{source} is listed but registers no capability"
            for source in row
            if source not in registered_for_row
        )
        violations.extend(
            f"unranked-leg: {domain}/{source} serves auto requests but is not listed"
            for source in auto_eligible.get(domain, set()) - set(row)
        )
    violations.extend(
        f"unlisted-domain: {domain} is served by {len(served[domain])} sources but has no row"
        for domain in served
        if domain not in declared and len(served[domain]) > 1
    )
    return tuple(sorted(violations))


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


@dataclass(frozen=True)
class ProviderModelDescriptor:
    """Immutable provider/model identity derived from a registered capability."""

    source: str
    model: str
    domain: str
    capability_identity: tuple[str, str, str, str, str]
    verified: bool

    @property
    def full_capability_identity(self) -> tuple[str, str, str, str, str]:
        """Expose the complete routing identity under an explicit name."""
        return self.capability_identity


def _validate_model_identity(source: str, model: str) -> None:
    """Reject malformed and auto-routed provider/model identities."""
    if (
        not isinstance(source, str)
        or not source.isidentifier()
        or keyword.iskeyword(source)
        or source.casefold() == "auto"
    ):
        raise ValueError(f"invalid provider source identity: {source!r}")
    if (
        not isinstance(model, str)
        or not model.isidentifier()
        or keyword.iskeyword(model)
        or model.casefold() == "auto"
    ):
        raise ValueError(f"invalid provider model identity: {model!r}")


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
        self._model_capability_keys: dict[tuple[str, str], str] = {}
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

    def register_model(self, source: str, model: str, fetcher: Fetcher[Any, Any]) -> None:
        """Bind one canonical provider/model identity to an already registered fetcher.

        Model identity is a second index into the capability registry, not a
        second fetcher store. The exact registered object must be supplied so
        this operation cannot silently alias another implementation.
        """
        capability_key, (model_id,), _ = self._validate_model_bindings(
            source, (model,), fetcher, allow_unregistered=False
        )
        self._model_capability_keys[(source, model_id)] = capability_key

    def register_provider_models(
        self,
        source: str,
        models: Iterable[str],
        fetcher: Fetcher[Any, Any],
    ) -> bool:
        """Register a fetcher and its canonical model aliases as one checked operation.

        Returns whether the capability itself was newly registered. Existing
        same-object capability/model bindings are idempotent; collisions are
        rejected before either the capability or model index is changed.
        """
        if isinstance(models, str):
            raise TypeError("models must be an iterable of model identifiers, not a string")
        model_ids = tuple(models)
        if not model_ids:
            raise ValueError("register_provider_models requires at least one model id")
        capability_key, validated_ids, add_capability = self._validate_model_bindings(
            source, model_ids, fetcher, allow_unregistered=True
        )
        if add_capability:
            self.register(fetcher)
        for model_id in validated_ids:
            self._model_capability_keys[(source, model_id)] = capability_key
        return add_capability

    def _validate_model_bindings(
        self,
        source: str,
        models: tuple[str, ...],
        fetcher: Fetcher[Any, Any],
        *,
        allow_unregistered: bool,
    ) -> tuple[str, tuple[str, ...], bool]:
        """Validate all aliases before mutating the capability/model maps."""
        if not models:
            raise ValueError("provider model bindings must not be empty")
        if len(models) != len(set(models)):
            raise ValueError("provider model bindings must not contain duplicate ids")
        for model in models:
            _validate_model_identity(source, model)

        capability = fetcher.capability
        if capability.source != source:
            raise ValueError(
                f"fetcher source {capability.source!r} does not match provider {source!r}"
            )
        capability_key = _capability_key(capability)
        registered_fetcher = self._fetchers.get(capability_key)
        add_capability = registered_fetcher is None
        if registered_fetcher is not fetcher and not (allow_unregistered and add_capability):
            raise ValueError("provider model binding requires the exact registered fetcher object")

        for model in models:
            existing_key = self._model_capability_keys.get((source, model))
            if existing_key is None:
                continue
            if existing_key != capability_key:
                raise ValueError(
                    f"provider model {source}/{model} is already bound to another capability"
                )
            if self._fetchers.get(existing_key) is not fetcher:
                raise ValueError(
                    "provider model binding requires the exact registered fetcher object"
                )
        return capability_key, models, add_capability

    def resolve_model(self, source: str, model: str) -> Fetcher[Any, Any]:
        """Resolve one explicit canonical provider/model identity without fallback."""
        _validate_model_identity(source, model)
        capability_key = self._model_capability_keys.get((source, model))
        if capability_key is None:
            raise LookupError(f"unknown provider model: {source}/{model}")
        fetcher = self._fetchers.get(capability_key)
        if fetcher is None:
            raise RuntimeError(
                f"provider model binding references missing capability: {source}/{model}"
            )
        return fetcher

    def list_model_descriptors(self) -> tuple[ProviderModelDescriptor, ...]:
        """Describe current provider/model bindings from the capability map."""
        descriptors: list[ProviderModelDescriptor] = []
        for (source, model), capability_key in self._model_capability_keys.items():
            fetcher = self._fetchers.get(capability_key)
            if fetcher is None:
                raise RuntimeError(
                    f"provider model binding references missing capability: {source}/{model}"
                )
            capability = fetcher.capability
            descriptors.append(
                ProviderModelDescriptor(
                    source=source,
                    model=model,
                    domain=capability.domain,
                    capability_identity=(
                        capability.asset_class,
                        capability.domain,
                        capability.period,
                        capability.market,
                        capability.source,
                    ),
                    verified=capability.verified,
                )
            )
        return tuple(descriptors)

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
            market: Optional market filter (e.g. ``cn``). Left as
                ``None`` on a domain whose verified legs span more than
                one market, routing refuses to guess (see
                :meth:`_reject_cross_market_auto`).
            source: ``"auto"`` or an explicit source name.

        Returns:
            The resolved fetcher.

        Raises:
            LookupError: If no capability matches, the explicit source
                is unknown, no auto candidate is healthy, or ``market``
                is omitted on a multi-market domain.
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
        if market is None:
            self._reject_cross_market_auto(asset_class, domain, ranked)
        missing_credentials: list[str] = []
        for key, fetcher in ranked:
            if not self._healthy.get(key, True):
                continue
            source_name = fetcher.capability.source
            credential_name = _credential_environment_name(source_name)
            if credential_name is not None and not _source_has_credentials(source_name):
                logger.warning(
                    "AUTO_ROUTE_CREDENTIAL_MISSING source=%s domain=%s credential=%s",
                    source_name,
                    domain,
                    credential_name,
                )
                missing_credentials.append(source_name)
                continue
            return fetcher
        if not ranked:
            raise LookupError(
                f"no verified capability registered for {asset_class}/{domain} "
                f"(period={period}, market={market})"
            )
        if missing_credentials:
            sources = ", ".join(dict.fromkeys(missing_credentials))
            raise LookupError(
                f"no credential-eligible verified auto candidate for {asset_class}/{domain}; "
                f"missing credentials for source(s): {sources}"
            )
        raise LookupError(f"all auto candidates for {asset_class}/{domain} are unavailable")

    def resolve_domain(
        self,
        domain: str,
        *,
        source: str = "auto",
        period: str | None = None,
        market: str | None = None,
    ) -> Fetcher[Any, Any]:
        """Route by domain identifier (catalog-facing, FR-17).

        Domains are unique across asset classes (domains.yaml), so
        matching by domain is unambiguous: the first capability found
        supplies the asset class for the full routing rules.
        ``period`` / ``market`` are forwarded rather than dropped -
        they are what tells the euro-area macro legs from the global
        ones, and a catalog caller that knows which one it wants has
        until now had no way to say so.

        Args:
            domain: Domain identifier, e.g. ``stock_daily``.
            source: ``"auto"`` or an explicit source name.
            period: Optional period filter (e.g. ``1M``).
            market: Optional market filter (e.g. ``eu``).

        Returns:
            The resolved fetcher.

        Raises:
            LookupError: If no capability is registered for the domain,
                or auto routing would have to pick across markets.
        """
        for fetcher in self._fetchers.values():
            if fetcher.capability.domain == domain:
                return self.resolve(
                    fetcher.capability.asset_class,
                    domain,
                    period=period,
                    market=market,
                    source=source,
                )
        raise LookupError(f"no capability registered for domain {domain!r}")

    @staticmethod
    def _reject_cross_market_auto(
        asset_class: str,
        domain: str,
        candidates: Sequence[tuple[str, Fetcher[Any, Any]]],
    ) -> None:
        """Refuse to answer a market-blind auto request with a lucky guess.

        ``economy_cpi`` is served by an ECB euro-area leg and an IMF
        global leg, and ``resolve_domain`` historically dropped the
        market dimension, so ``source="auto"`` reached whichever one
        happened to register first - a US question silently answered
        with euro-area HICP. An authority order cannot fix that: it
        only picks which market to answer with. The choice belongs to
        the caller, so the request fails closed and names the markets
        on offer.

        Args:
            asset_class: The requested asset class, for the message.
            domain: The requested domain, for the message.
            candidates: The auto-eligible candidates already filtered
                by the request's period.
        """
        markets = sorted({fetcher.capability.market for _, fetcher in candidates})
        if len(markets) < 2:
            return
        raise LookupError(
            f"{asset_class}/{domain} is served by {len(markets)} markets ({', '.join(markets)}) "
            f"and the request named none of them; source='auto' does not pick across "
            f"markets - pass market= (or an explicit source)"
        )

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
