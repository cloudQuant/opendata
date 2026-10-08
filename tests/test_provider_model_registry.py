"""Canonical provider/model identities index registered fetchers safely."""

from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from opendata.data.capability import Capability
from opendata.data.provider import LazyFetcherBinding, Provider
from opendata.data.providers import catalog
from opendata.data.registry import ProviderRegistry


class _Fetcher:
    def __init__(self, capability: Capability) -> None:
        self.capability = capability


def _fetcher(
    source: str,
    domain: str,
    *,
    period: str = "1D",
    market: str = "cn",
    verified: bool = False,
) -> _Fetcher:
    return _Fetcher(
        Capability(
            asset_class="equity",
            domain=domain,
            period=period,
            market=market,
            source=source,
            verified=verified,
        )
    )


def _install_provider(
    monkeypatch: pytest.MonkeyPatch,
    source: str,
    fetcher: _Fetcher,
    canonical_model_ids: tuple[str, ...],
) -> Provider:
    provider = Provider(
        source=source,
        name=source,
        description="Test provider descriptor.",
        fetcher_bindings=(
            LazyFetcherBinding(
                f"opendata.data.providers.{source}.models.fixture",
                "FixtureFetcher",
                canonical_model_ids=canonical_model_ids,
            ),
        ),
    )
    # Use the descriptor's normal cached fetcher view without importing a model.
    provider.__dict__["fetchers"] = (fetcher,)
    monkeypatch.setitem(catalog._PROVIDERS_BY_SOURCE, source, provider)
    return provider


def test_provider_derives_model_aliases_from_one_binding_and_registers_idempotently(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = "fixture"
    fetcher = _fetcher(source, "financial_statement")
    provider = _install_provider(monkeypatch, source, fetcher, ("BalanceSheet", "CashFlow"))

    assert set(provider.fetcher_dict) == {"BalanceSheet", "CashFlow"}
    assert provider.fetcher_dict["BalanceSheet"] is provider.fetcher_bindings[0]
    with pytest.raises(TypeError):
        provider.fetcher_dict["OtherModel"] = provider.fetcher_bindings[0]  # type: ignore[index]
    registry = ProviderRegistry()

    first = catalog.register_provider(source, registry)
    second = catalog.register_provider(source, registry)

    assert first == [fetcher.capability]
    assert second == []
    assert registry.resolve_model(source, "BalanceSheet") is fetcher
    assert registry.resolve_model(source, "CashFlow") is fetcher
    assert registry.capabilities() == [fetcher.capability]


def test_model_descriptor_keeps_model_granularity_separate_from_capability_domain() -> None:
    registry = ProviderRegistry()
    fetcher = _fetcher("alpha", "financial_statement", verified=False)
    registry.register(fetcher)
    registry.register_model("alpha", "BalanceSheet", fetcher)
    registry.register_model("alpha", "CashFlow", fetcher)

    descriptors = registry.list_model_descriptors()

    assert [(item.source, item.model, item.domain) for item in descriptors] == [
        ("alpha", "BalanceSheet", "financial_statement"),
        ("alpha", "CashFlow", "financial_statement"),
    ]
    assert all(
        item.capability_identity == ("equity", "financial_statement", "1D", "cn", "alpha")
        for item in descriptors
    )
    assert all(item.full_capability_identity == item.capability_identity for item in descriptors)
    assert all(item.verified is False for item in descriptors)
    with pytest.raises(FrozenInstanceError):
        descriptors[0].domain = "balance_sheet"  # type: ignore[misc]


def test_same_model_name_is_allowed_across_sources() -> None:
    registry = ProviderRegistry()
    alpha = _fetcher("alpha", "financial_statement")
    beta = _fetcher("beta", "balance_sheet")
    registry.register(alpha)
    registry.register(beta)

    registry.register_model("alpha", "BalanceSheet", alpha)
    registry.register_model("beta", "BalanceSheet", beta)

    assert registry.resolve_model("alpha", "BalanceSheet") is alpha
    assert registry.resolve_model("beta", "BalanceSheet") is beta


def test_same_provider_model_cannot_be_rebound_to_another_capability() -> None:
    registry = ProviderRegistry()
    original = _fetcher("alpha", "financial_statement")
    conflicting = _fetcher("alpha", "cash_flow")
    registry.register(original)
    registry.register(conflicting)
    registry.register_model("alpha", "BalanceSheet", original)

    with pytest.raises(ValueError, match="already bound to another capability"):
        registry.register_model("alpha", "BalanceSheet", conflicting)

    assert registry.resolve_model("alpha", "BalanceSheet") is original


def test_model_registration_requires_the_exact_registered_fetcher_and_source() -> None:
    registry = ProviderRegistry()
    fetcher = _fetcher("alpha", "financial_statement")
    registry.register(fetcher)
    different_object = _fetcher("alpha", "financial_statement")

    with pytest.raises(ValueError, match="exact registered fetcher object"):
        registry.register_model("alpha", "BalanceSheet", different_object)
    with pytest.raises(ValueError, match="does not match provider"):
        registry.register_model("beta", "BalanceSheet", fetcher)
    with pytest.raises(ValueError, match="exact registered fetcher object"):
        registry.register_model("alpha", "BalanceSheet", _fetcher("alpha", "financial_statement"))
    with pytest.raises(ValueError, match="exact registered fetcher object"):
        registry.register_model("alpha", "UnregisteredModel", _fetcher("alpha", "unregistered"))
    assert registry.capabilities() == [fetcher.capability]


def test_unknown_and_auto_model_routes_fail_without_legacy_fallback() -> None:
    registry = ProviderRegistry()
    fetcher = _fetcher("alpha", "financial_statement")
    registry.register(fetcher)
    registry.register_model("alpha", "BalanceSheet", fetcher)

    with pytest.raises(LookupError, match="unknown provider model"):
        registry.resolve_model("missing", "BalanceSheet")
    with pytest.raises(LookupError, match="unknown provider model"):
        registry.resolve_model("alpha", "MissingModel")
    with pytest.raises(ValueError, match="invalid provider source"):
        registry.resolve_model("auto", "BalanceSheet")
    with pytest.raises(ValueError, match="invalid provider model"):
        registry.resolve_model("alpha", "auto")


@pytest.mark.parametrize("model_ids", [("",), ("bad.id",), ("auto",), ("class",), ("Same", "Same")])
def test_lazy_binding_rejects_invalid_or_duplicate_model_ids(
    model_ids: tuple[str, ...],
) -> None:
    with pytest.raises(ValueError):
        LazyFetcherBinding("unused.module", "Fetcher", canonical_model_ids=model_ids)


def test_provider_rejects_duplicate_model_ids_across_bindings() -> None:
    with pytest.raises(ValueError, match="duplicate fetcher model ids"):
        Provider(
            source="fixture",
            name="Fixture",
            description="Test provider descriptor.",
            fetcher_bindings=(
                LazyFetcherBinding("fixture.models.one", "OneFetcher", ("SharedModel",)),
                LazyFetcherBinding("fixture.models.two", "TwoFetcher", ("SharedModel",)),
            ),
        )


def test_provider_registration_collision_does_not_steal_model_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = "fixture"
    original = _fetcher(source, "financial_statement")
    conflicting = _fetcher(source, "cash_flow")
    registry = ProviderRegistry()
    _install_provider(monkeypatch, source, original, ("BalanceSheet",))
    catalog.register_provider(source, registry)

    _install_provider(monkeypatch, source, conflicting, ("BalanceSheet",))
    with pytest.raises(ValueError, match="already bound to another capability"):
        catalog.register_provider(source, registry)

    assert registry.resolve_model(source, "BalanceSheet") is original
    assert len(registry.capabilities()) == 1


def test_provider_registration_rejects_a_different_object_for_existing_capability(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = "fixture"
    original = _fetcher(source, "financial_statement")
    registry = ProviderRegistry()
    _install_provider(monkeypatch, source, original, ("BalanceSheet",))
    catalog.register_provider(source, registry)

    replacement = _fetcher(source, "financial_statement")
    _install_provider(monkeypatch, source, replacement, ("BalanceSheet",))
    with pytest.raises(ValueError, match="exact registered fetcher object"):
        catalog.register_provider(source, registry)

    assert registry.resolve_model(source, "BalanceSheet") is original
    assert registry.capabilities() == [original.capability]
