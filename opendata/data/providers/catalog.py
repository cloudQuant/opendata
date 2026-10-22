"""Built-in provider metadata, local health checks, and registry assembly.

Importing this module reads only the provider descriptors. Fetcher modules,
settings, SDKs, database clients, schedulers, and network transports are loaded
only by their explicit operations.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from opendata.data.provider import CredentialSpec, Provider, ProviderHealth
from opendata.data.providers.akshare.provider import PROVIDER as AKSHARE_PROVIDER
from opendata.data.providers.alpha_vantage.provider import PROVIDER as ALPHA_VANTAGE_PROVIDER
from opendata.data.providers.benzinga.provider import PROVIDER as BENZINGA_PROVIDER
from opendata.data.providers.biztoc.provider import PROVIDER as BIZTOC_PROVIDER
from opendata.data.providers.bls.provider import PROVIDER as BLS_PROVIDER
from opendata.data.providers.cboe.provider import PROVIDER as CBOE_PROVIDER
from opendata.data.providers.cftc.provider import PROVIDER as CFTC_PROVIDER
from opendata.data.providers.congress_gov.provider import PROVIDER as CONGRESS_GOV_PROVIDER
from opendata.data.providers.deribit.provider import PROVIDER as DERIBIT_PROVIDER
from opendata.data.providers.ecb.provider import PROVIDER as ECB_PROVIDER
from opendata.data.providers.econdb.provider import PROVIDER as ECONDB_PROVIDER
from opendata.data.providers.eia.provider import PROVIDER as EIA_PROVIDER
from opendata.data.providers.famafrench.provider import PROVIDER as FAMAFRENCH_PROVIDER
from opendata.data.providers.federal_reserve.provider import PROVIDER as FEDERAL_RESERVE_PROVIDER
from opendata.data.providers.finra.provider import PROVIDER as FINRA_PROVIDER
from opendata.data.providers.finviz.provider import PROVIDER as FINVIZ_PROVIDER
from opendata.data.providers.fmp.provider import PROVIDER as FMP_PROVIDER
from opendata.data.providers.fred.provider import PROVIDER as FRED_PROVIDER
from opendata.data.providers.government_us.provider import PROVIDER as GOVERNMENT_US_PROVIDER
from opendata.data.providers.imf.provider import PROVIDER as IMF_PROVIDER
from opendata.data.providers.intrinio.provider import PROVIDER as INTRINIO_PROVIDER
from opendata.data.providers.multpl.provider import PROVIDER as MULTPL_PROVIDER
from opendata.data.providers.nasdaq.provider import PROVIDER as NASDAQ_PROVIDER
from opendata.data.providers.oecd.provider import PROVIDER as OECD_PROVIDER
from opendata.data.providers.sec.provider import PROVIDER as SEC_PROVIDER
from opendata.data.providers.seeking_alpha.provider import (
    PROVIDER as SEEKING_ALPHA_PROVIDER,
)
from opendata.data.providers.ths.provider import PROVIDER as THS_PROVIDER
from opendata.data.providers.tiingo.provider import PROVIDER as TIINGO_PROVIDER
from opendata.data.providers.tmx.provider import PROVIDER as TMX_PROVIDER
from opendata.data.providers.tradier.provider import PROVIDER as TRADIER_PROVIDER
from opendata.data.providers.tradingeconomics.provider import (
    PROVIDER as TRADINGECONOMICS_PROVIDER,
)
from opendata.data.providers.wsj.provider import PROVIDER as WSJ_PROVIDER
from opendata.data.providers.yfinance.provider import PROVIDER as YFINANCE_PROVIDER

if TYPE_CHECKING:
    from opendata.data.capability import Capability
    from opendata.data.protocol import Fetcher
    from opendata.data.providers._engine.spec import ModelSpec
    from opendata.data.registry import ProviderRegistry


def _credential(name: str, provider_source: str) -> CredentialSpec:
    """Describe a declared key without claiming it is required for health."""
    environment_variable = f"{provider_source.upper()}_{name.upper()}"
    return CredentialSpec(name=name, environment_variable=environment_variable)


def _reserved_provider(source: str, *credential_names: str) -> Provider:
    """Create metadata for a source with no local fetcher implementation."""
    return Provider(
        source=source,
        name=source.replace("_", " ").title(),
        description="Catalog identity reserved; no local fetcher is implemented.",
        credentials=tuple(_credential(name, source) for name in credential_names),
    )


# The 32 upstream inventory identities are recorded in their inventory order.
# This metadata does not import, install, or probe any upstream implementation.
_UPSTREAM_PROVIDERS: tuple[Provider, ...] = (
    ECB_PROVIDER,
    FRED_PROVIDER,
    IMF_PROVIDER,
    OECD_PROVIDER,
    YFINANCE_PROVIDER,
    ALPHA_VANTAGE_PROVIDER,
    BLS_PROVIDER,
    ECONDB_PROVIDER,
    EIA_PROVIDER,
    FAMAFRENCH_PROVIDER,
    FEDERAL_RESERVE_PROVIDER,
    FMP_PROVIDER,
    INTRINIO_PROVIDER,
    MULTPL_PROVIDER,
    SEEKING_ALPHA_PROVIDER,
    TIINGO_PROVIDER,
    TRADINGECONOMICS_PROVIDER,
    BENZINGA_PROVIDER,
    BIZTOC_PROVIDER,
    CBOE_PROVIDER,
    CFTC_PROVIDER,
    CONGRESS_GOV_PROVIDER,
    DERIBIT_PROVIDER,
    FINRA_PROVIDER,
    FINVIZ_PROVIDER,
    GOVERNMENT_US_PROVIDER,
    NASDAQ_PROVIDER,
    SEC_PROVIDER,
    _reserved_provider("stockgrid"),
    TMX_PROVIDER,
    TRADIER_PROVIDER,
    WSJ_PROVIDER,
)

PROVIDERS: tuple[Provider, ...] = (
    *_UPSTREAM_PROVIDERS,
    THS_PROVIDER,
    AKSHARE_PROVIDER,
)
_PROVIDERS_BY_SOURCE = {provider.source: provider for provider in PROVIDERS}
# Preserve the legacy capability registration order for callers that observe
# registry iteration order (for example, the capabilities API).
_LEGACY_REGISTRATION_ORDER = (
    AKSHARE_PROVIDER,
    ECB_PROVIDER,
    FRED_PROVIDER,
    IMF_PROVIDER,
    OECD_PROVIDER,
    THS_PROVIDER,
    YFINANCE_PROVIDER,
    BLS_PROVIDER,
    FMP_PROVIDER,
)


def registration_order() -> tuple[Provider, ...]:
    """Return every locally implemented provider in the order it registers.

    The legacy tuple is kept verbatim because callers observe registry iteration order, and
    anything implemented but not named there is appended in ``PROVIDERS`` order. Deriving the tail
    is the point: a hand-maintained registration list is how a declared model ends up invisible to
    the runtime that is supposed to route it, and the omission stays green because nothing asks.
    """
    listed = {provider.source for provider in _LEGACY_REGISTRATION_ORDER}
    return _LEGACY_REGISTRATION_ORDER + tuple(
        provider
        for provider in PROVIDERS
        if provider.is_implemented and provider.source not in listed
    )


def list_providers() -> tuple[Provider, ...]:
    """Return the 34 built-in descriptors without loading source models."""
    return PROVIDERS


def get_provider(source: str) -> Provider:
    """Return one built-in provider descriptor by its stable source id."""
    try:
        return _PROVIDERS_BY_SOURCE[source]
    except KeyError as exc:
        raise KeyError(f"unknown provider source: {source!r}") from exc


def health_check(source: str | None = None) -> dict[str, ProviderHealth]:
    """Check local configuration and optional imports without network access.

    With no source, all provider statuses are returned. Unimplemented sources
    are classified before checking their declared credentials or dependencies.
    """
    providers = PROVIDERS if source is None else (get_provider(source),)
    return {provider.source: provider.health_check() for provider in providers}


def _capability_identity(capability: Capability) -> tuple[str, str, str, str, str]:
    """Match the full routing key used by ``ProviderRegistry``."""
    return (
        capability.asset_class,
        capability.domain,
        capability.period,
        capability.market,
        capability.source,
    )


def register_provider(
    source: str,
    registry: ProviderRegistry | None = None,
) -> list[Capability]:
    """Register one provider's fetchers, idempotent on their full routing key."""
    provider = get_provider(source)
    if registry is None:
        from opendata.data.registry import get_registry

        registry = get_registry()

    existing = {_capability_identity(capability) for capability in registry.capabilities()}
    registered: list[Capability] = []
    for binding, fetcher in zip(provider.fetcher_bindings, provider.fetchers, strict=True):
        capability = fetcher.capability
        identity = _capability_identity(capability)
        if binding.canonical_model_ids:
            if registry.register_provider_models(
                provider.source, binding.canonical_model_ids, fetcher
            ):
                existing.add(identity)
                registered.append(capability)
            continue
        if identity not in existing:
            registry.register(fetcher)
            existing.add(identity)
            registered.append(capability)
    return registered


def fetchers_for(source: str) -> tuple[Fetcher[Any, Any], ...]:
    """Project the compatibility ``FETCHERS`` tuple from provider metadata."""
    return get_provider(source).fetchers


def engine_declared_models() -> list[tuple[str, ModelSpec]]:
    """Discover every declarative engine model through the runtime registration truth.

    Discovery walks the provider descriptors and their lazy bindings -- the same objects the
    registry stores -- because a hand-maintained second list of models is the failure AC2-02
    refuses: a derived table can drift from the runtime it claims to describe. The engine type is
    imported here rather than at module scope so importing the catalog still loads only provider
    descriptors.
    """
    from opendata.data.providers._engine.spec import ModelSpec

    found: list[tuple[str, ModelSpec]] = []
    for descriptor in PROVIDERS:
        for binding in descriptor.fetcher_bindings:
            spec = getattr(binding.load(descriptor.source), "model_spec", None)
            if isinstance(spec, ModelSpec):
                found.append((descriptor.source, spec))
    return sorted(found, key=lambda pair: f"{pair[0]}::{pair[1].model}")


def register_providers(
    registry: ProviderRegistry | None = None,
) -> list[Capability]:
    """Register every locally implemented source; reserved entries stay metadata-only."""
    if registry is None:
        from opendata.data.registry import get_registry

        registry = get_registry()
    registered: list[Capability] = []
    for provider in registration_order():
        registered.extend(register_provider(provider.source, registry))
    return registered


__all__ = [
    "PROVIDERS",
    "engine_declared_models",
    "fetchers_for",
    "get_provider",
    "health_check",
    "list_providers",
    "register_provider",
    "register_providers",
    "registration_order",
]
