"""Built-in provider metadata, local health checks, and registry assembly.

Importing this module reads only the provider descriptors. Fetcher modules,
settings, SDKs, database clients, schedulers, and network transports are loaded
only by their explicit operations.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from opendata.data.provider import CredentialSpec, Provider, ProviderHealth
from opendata.data.providers.akshare.provider import PROVIDER as AKSHARE_PROVIDER
from opendata.data.providers.bls.provider import PROVIDER as BLS_PROVIDER
from opendata.data.providers.cboe.provider import PROVIDER as CBOE_PROVIDER
from opendata.data.providers.ecb.provider import PROVIDER as ECB_PROVIDER
from opendata.data.providers.fmp.provider import PROVIDER as FMP_PROVIDER
from opendata.data.providers.fred.provider import PROVIDER as FRED_PROVIDER
from opendata.data.providers.imf.provider import PROVIDER as IMF_PROVIDER
from opendata.data.providers.oecd.provider import PROVIDER as OECD_PROVIDER
from opendata.data.providers.ths.provider import PROVIDER as THS_PROVIDER
from opendata.data.providers.yfinance.provider import PROVIDER as YFINANCE_PROVIDER

if TYPE_CHECKING:
    from opendata.data.capability import Capability
    from opendata.data.protocol import Fetcher
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
    _reserved_provider("alpha_vantage", "api_key"),
    BLS_PROVIDER,
    _reserved_provider("econdb", "api_key"),
    _reserved_provider("eia", "api_key"),
    _reserved_provider("famafrench"),
    _reserved_provider("federal_reserve"),
    FMP_PROVIDER,
    _reserved_provider("intrinio", "api_key"),
    _reserved_provider("multpl"),
    _reserved_provider("seeking_alpha"),
    _reserved_provider("tiingo", "token"),
    _reserved_provider("tradingeconomics", "api_key"),
    _reserved_provider("benzinga", "api_key"),
    _reserved_provider("biztoc", "api_key"),
    CBOE_PROVIDER,
    _reserved_provider("cftc", "app_token"),
    _reserved_provider("congress_gov", "api_key"),
    _reserved_provider("deribit"),
    _reserved_provider("finra"),
    _reserved_provider("finviz"),
    _reserved_provider("government_us"),
    _reserved_provider("nasdaq", "api_key"),
    _reserved_provider("sec"),
    _reserved_provider("stockgrid"),
    _reserved_provider("tmx"),
    _reserved_provider("tradier", "api_key", "account_type"),
    _reserved_provider("wsj"),
)

PROVIDERS: tuple[Provider, ...] = (
    *_UPSTREAM_PROVIDERS,
    THS_PROVIDER,
    AKSHARE_PROVIDER,
)
_PROVIDERS_BY_SOURCE = {provider.source: provider for provider in PROVIDERS}
# Preserve the legacy capability registration order for callers that observe
# registry iteration order (for example, the capabilities API).
_REGISTRATION_ORDER = (
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


def register_providers(
    registry: ProviderRegistry | None = None,
) -> list[Capability]:
    """Register every locally implemented source; reserved entries stay metadata-only."""
    if registry is None:
        from opendata.data.registry import get_registry

        registry = get_registry()
    registered: list[Capability] = []
    for provider in _REGISTRATION_ORDER:
        registered.extend(register_provider(provider.source, registry))
    return registered


__all__ = [
    "PROVIDERS",
    "fetchers_for",
    "get_provider",
    "health_check",
    "list_providers",
    "register_provider",
    "register_providers",
]
