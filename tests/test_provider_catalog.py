"""Provider catalog metadata, lazy-import boundaries, and compatibility."""

from __future__ import annotations

import builtins
import importlib
import os
import socket
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest

from opendata.data.provider import LazyFetcherBinding, Provider
from opendata.data.providers._engine.spec import ModelSpec
from opendata.data.providers.catalog import (
    engine_declared_models,
    get_provider,
    health_check,
    list_providers,
    register_providers,
    registration_order,
)
from opendata.data.registry import ProviderRegistry

if TYPE_CHECKING:
    from opendata.data.capability import Capability

EXPECTED_SOURCES = {
    "ecb",
    "fred",
    "imf",
    "oecd",
    "yfinance",
    "alpha_vantage",
    "bls",
    "econdb",
    "eia",
    "famafrench",
    "federal_reserve",
    "fmp",
    "intrinio",
    "multpl",
    "seeking_alpha",
    "tiingo",
    "tradingeconomics",
    "benzinga",
    "biztoc",
    "cboe",
    "cftc",
    "congress_gov",
    "deribit",
    "finra",
    "finviz",
    "government_us",
    "nasdaq",
    "sec",
    "stockgrid",
    "tmx",
    "tradier",
    "wsj",
    "ths",
    "akshare",
}


def _capability_identity(capability: Capability) -> tuple[str, str, str, str, str]:
    return (
        capability.asset_class,
        capability.domain,
        capability.period,
        capability.market,
        capability.source,
    )


class _CapabilityOnlyFetcher:
    def __init__(self, capability: Capability) -> None:
        self.capability = capability


def test_catalog_contains_34_sources_and_the_live_fetcher_binding_census() -> None:
    """The catalog binding census, split into the two populations that make it up.

    The freeze started at 66 bindings (48 hand-written + 18 declarative engine models); today it is
    372 (355 hand-written + 17 engine). The split is counted
    apart and the sum is still frozen, because the two kinds of source work grow for different
    reasons: a new hand-written fetcher is one provider's own module, while a declaration adds a
    binding *and* a generated fetcher, and an engine model that registered without a binding
    would be invisible to the runtime while still showing up in the spec census. Which binding is
    engine-driven is read the way the runtime reads it -- a fetcher carrying ``model_spec`` -- not
    from a hand-maintained id list, so the count cannot be kept green by editing a list.

    How the 66 is built, since a frozen sum hides a retraction: 46 at the wave-1 baseline, +4 from
    the cboe declarations, -2 for the oecd rows that shipped as hand-written fetchers first and had
    their declarations withdrawn, +2 for the federal_reserve CSV rows, +1 for the third
    federal_reserve row declared through ``ModelSpec.row_expansion``, +3 for the three intrinio
    declarations, and +1 for intrinio's fourth row (``CalendarIpo``), also declared through
    ``ModelSpec.row_expansion``, +2 for the two federal_reserve rows this round
    (``FederalFundsRate`` and ``SOFR``) and +1 for intrinio's ``InsiderTrading``; this
    wave adds +1 for federal_reserve's ``OvernightBankFundingRate`` and +3 for the three
    packages it registers at once (``benzinga_price_target``, ``tiingo_crypto_historical``,
    ``econdb_available_indicators``). A withdrawn
    declaration is counted out rather than left to register, so the assert freezes the population
    the runtime can actually route. The
    hand-written leg rose from 44 to 48 with four Finviz bindings, while the declared leg went
    9 -> 10 -> 11 -> 14 -> 18:
    intrinio's third
    candidate row (``HistoricalDividends``) is no longer withheld -- its spec keeps it in
    ``providers/intrinio/specs.py`` as ``HISTORICAL_DIVIDENDS`` with an empty ``NOT_DECLARABLE`` --
    Waves 33-39 added 30 hand-written bindings on top of that freeze (355 hand + 17 engine
    today), so the literal below is the live census with its per-provider breakdown in the
    comment above the assertions rather than a hand-kept budget.
    and its fourth row adds the binding plus the fetcher the engine generates for each of them.
    """
    providers = list_providers()
    bindings = [
        (provider.source, binding)
        for provider in providers
        for binding in provider.fetcher_bindings
    ]

    assert len(providers) == 34
    assert {provider.source for provider in providers} == EXPECTED_SOURCES
    # Re-pinned after 360c61d (32-provider implementation surface): 14 sources moved from
    # reserved to implemented, and the binding census grew 66 -> 342 with that wave plus the
    # iter2 declarative/engine rows (split then: 325 hand-written facade fetchers + 17 engine).
    # Wave 38 moves one name from reserved to implemented: seeking_alpha's calendar leg.
    # Re-pinned after waves 33-39: +30 hand-written bindings from those waves' landed rows, so the
    # live split is 355 hand-written + 17 engine = 372. Per-provider census today:
    # fmp 69, fred 39, intrinio 37, yfinance 29, sec 24, tmx 24, federal_reserve 13, imf 11,
    # oecd 11, cboe 11, ths 11, akshare 10, nasdaq 9, congress_gov 8, tiingo 7, finviz 7, ecb 6,
    # econdb 6, famafrench 6, government_us 6, deribit 5, tradier 5, benzinga 4, alpha_vantage 3,
    # bls 2, eia 2, cftc 2, finra 2, multpl 1, seeking_alpha 1, tradingeconomics 1,
    # biztoc 0, stockgrid 0, wsj 0.

    assert {provider.source for provider in providers if provider.is_implemented} == {
        "akshare",
        "alpha_vantage",
        "benzinga",
        "bls",
        "cboe",
        "cftc",
        "congress_gov",
        "deribit",
        "ecb",
        "econdb",
        "eia",
        "famafrench",
        "federal_reserve",
        "finra",
        "finviz",
        "fmp",
        "fred",
        "government_us",
        "imf",
        "intrinio",
        "multpl",
        "nasdaq",
        "oecd",
        "sec",
        "seeking_alpha",
        "ths",
        "tiingo",
        "tmx",
        "tradier",
        "tradingeconomics",
        "wsj",
        "yfinance",
        "biztoc",
    }
    declared_ids = {f"{source}::{spec.model}" for source, spec in engine_declared_models()}
    engine_bindings = [
        (source, binding)
        for source, binding in bindings
        if binding.canonical_model_ids
        and isinstance(getattr(binding.load(source), "model_spec", None), ModelSpec)
    ]
    engine_ids = {
        f"{source}::{binding.canonical_model_ids[0]}" for source, binding in engine_bindings
    }
    assert engine_ids == declared_ids, "an engine binding's canonical id is the model it generates"
    assert len(bindings) == 379
    assert len(bindings) - len(engine_bindings) == 359
    assert len(engine_bindings) == len(bindings) - 359
    print(
        f"\ncatalog bindings: total={len(bindings)} engine={len(engine_bindings)} "
        f"hand_written={len(bindings) - len(engine_bindings)} engine_ids={sorted(engine_ids)}"
    )
    implemented = [provider for provider in providers if provider.is_implemented]
    assert all(
        provider.website and provider.website.startswith("https://") for provider in implemented
    )

    reserved = [provider for provider in providers if not provider.is_implemented]
    # 3, not 4: seeking_alpha moved from reserved to implemented in wave 38, leaving
    # biztoc / stockgrid / wsj as the headless packages.
    assert len(reserved) == 1
    assert all(provider.health_check().status == "not_implemented" for provider in reserved)
    assert all(not provider.fetcher_bindings for provider in reserved)


def test_metadata_import_does_not_load_configuration_models_or_network() -> None:
    script = r"""
import builtins
import socket
import sys

blocked = (
    "opendata.core.config",
    "sqlalchemy",
    "apscheduler",
    "yfinance",
    "akshare",
    "opendata_http",
    "opendata_fuyao",
    "opendata.data.providers.akshare._vendor",
    "opendata.data.providers.ths.transport",
    "pandas",
    "httpx",
)
real_import = builtins.__import__

def guarded_import(name, globals=None, locals=None, fromlist=(), level=0):
    if any(name == prefix or name.startswith(prefix + ".") for prefix in blocked):
        raise AssertionError("forbidden import: " + name)
    if name.startswith("opendata.data.providers.") and ".models" in name:
        raise AssertionError("source model imported: " + name)
    return real_import(name, globals, locals, fromlist, level)

def reject_network(*args, **kwargs):
    raise AssertionError("metadata attempted network access")

builtins.__import__ = guarded_import
socket.socket.connect = reject_network
socket.create_connection = reject_network

import opendata
from opendata.data.providers.catalog import list_providers

assert "settings" not in opendata.__dict__
assert len(list_providers()) == 34
assert "opendata.core.config" not in sys.modules
assert "pandas" not in sys.modules
assert "httpx" not in sys.modules
assert not any(
    name.startswith("opendata.data.providers.") and ".models" in name
    for name in sys.modules
)
"""
    result = subprocess.run(  # noqa: S603 - isolated test interpreter runs fixed source
        [sys.executable, "-c", script],
        cwd=Path(__file__).resolve().parents[1],
        env=os.environ.copy(),
        capture_output=True,
        check=False,
        text=True,
    )

    assert result.returncode == 0, f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"


def test_registration_and_legacy_fetchers_project_the_same_capabilities() -> None:
    """The registry's capability set, rebuilt from the two populations separately.

    ``expected_counts`` freezes the hand-written fetchers each provider's compatibility facade
    publishes. A facade whose source also carries engine declarations answers ``FETCHERS`` with both
    kinds, so the two are split on the same marker the runtime uses (a fetcher carrying
    ``model_spec``) before either count is read, and the split sources are printed as a census. That
    census measures empty today: oecd's two retracted ledger rows left its facade hand-written only,
    and the ten registered declarations sit in sources with no hand-written fetcher at all. Folding
    the kinds together would double-count a declaration's identity and turn the disjointness assert
    below into a tautology: an identity shared between the hand-written leg and the declared leg is
    the sign that a declaration replaced a fetcher, which is a different claim from the one this
    test makes.
    """
    registry = ProviderRegistry()
    registered = register_providers(registry)
    source_fetchers = []
    mixed_facades: list[str] = []
    expected_counts = {
        # Re-pinned after 360c61d: hand-written facade FETCHERS grew additively for fred
        # (7->39), imf (3->11), yfinance (1->28) and fmp (2->69); the other five are unchanged.
        # Re-pinned again after waves 33-39: oecd 2->11 and yfinance 28->29; both providers'
        # inventory rows are served by hand-written adapters now, and the measured facade counts
        # are 11 and 29. The other seven are unchanged.
        "akshare": 10,
        "ecb": 6,
        "fred": 39,
        "imf": 11,
        "oecd": 11,
        "ths": 11,
        "yfinance": 30,
        "bls": 2,
        "fmp": 69,
    }

    for source, count in expected_counts.items():
        registration = importlib.import_module(f"opendata.data.providers.{source}.registration")
        package = importlib.import_module(f"opendata.data.providers.{source}")
        provider = get_provider(source)
        fetchers = registration.FETCHERS
        hand_written = tuple(
            fetcher
            for fetcher in fetchers
            if not isinstance(getattr(fetcher, "model_spec", None), ModelSpec)
        )
        declared_here = len(fetchers) - len(hand_written)
        assert len(hand_written) == count, source
        assert len(provider.fetcher_dict) == len(fetchers)
        assert tuple(provider.fetcher_dict.values()) == provider.fetcher_bindings
        assert package.register is registration.register
        if source != "akshare":
            assert fetchers == package.FETCHERS
        if declared_here:
            mixed_facades.append(f"{source}=hand_written:{count}+engine:{declared_here}")
        source_fetchers.extend(hand_written)

    print(f"\nmixed hand-written/declared facades: {mixed_facades or ['none']}")

    expected = {_capability_identity(fetcher.capability) for fetcher in source_fetchers}
    actual = {_capability_identity(capability) for capability in registry.capabilities()}
    # The declarations are a second, derived population: their count is what the catalog publishes,
    # and an identity they share with a hand-written leg would mean a declaration replaced it.
    declared_models = engine_declared_models()
    declared = {
        _capability_identity(registry.resolve_model(source, spec.model).capability)
        for source, spec in declared_models
    }
    # Census re-base 2026-10-14: the third leg is the live fetcher_bindings of every provider
    # whose source has no hand-written facade above.
    binding_legs = {
        provider.source: {
            _capability_identity(binding.load(provider.source).capability)
            for binding in provider.fetcher_bindings
        }
        for provider in registration_order()
        if provider.fetcher_bindings and provider.source not in expected_counts
    }
    declarative_bindings = set().union(*binding_legs.values())
    print(
        f"\ncapability census: hand_written={len(expected)} engine_declared={len(declared)} "
        f"declarative_bindings={len(declarative_bindings)} over {len(binding_legs)} sources "
        f"registered={len(registered)}"
    )
    expected_registration_order = [
        provider.source for provider in registration_order() for _ in provider.fetcher_bindings
    ]
    assert len(expected) == len(source_fetchers)
    assert len(declared) == len(declared_models)
    assert len(registered) == len(expected | declared | declarative_bindings)
    assert actual == expected | declared | declarative_bindings
    assert expected & declared == set()
    assert [capability.source for capability in registered] == expected_registration_order
    assert register_providers(registry) == []
    # live counterfact: a dropped declarative binding must break the exact census identity.
    unique_binding = next(iter(declarative_bindings - expected - declared))
    assert len(registered) != len(expected | declared | (declarative_bindings - {unique_binding}))


@pytest.mark.parametrize("field,value", [("period", "1W"), ("market", "us")])
def test_source_register_idempotence_uses_full_routing_identity(field: str, value: str) -> None:
    first_fetcher = get_provider("ths").fetchers[0]
    original = first_fetcher.capability
    changed = original.model_copy(update={field: value})
    registry = ProviderRegistry()
    registry.register(_CapabilityOnlyFetcher(changed))  # type: ignore[arg-type]

    registration = importlib.import_module("opendata.data.providers.ths.registration")
    added = registration.register(registry)

    assert len(added) == 11
    assert _capability_identity(original) in {
        _capability_identity(capability) for capability in registry.capabilities()
    }
    assert _capability_identity(changed) in {
        _capability_identity(capability) for capability in registry.capabilities()
    }
    assert registration.register(registry) == []


def test_missing_yfinance_sdk_does_not_change_other_provider_health(monkeypatch) -> None:
    import opendata.data.provider as provider_module

    original_find_spec = provider_module.importlib.util.find_spec

    def find_spec(import_name: str):
        if import_name == "yfinance":
            return None
        return original_find_spec(import_name)

    real_import = builtins.__import__

    def guarded_import(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "yfinance":
            raise AssertionError("health check imported the optional SDK")
        return real_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(provider_module.importlib.util, "find_spec", find_spec)
    monkeypatch.setattr(builtins, "__import__", guarded_import)

    def reject_network(*args, **kwargs):
        raise AssertionError("health check attempted network access")

    monkeypatch.setattr(socket.socket, "connect", reject_network)

    assert health_check("yfinance")["yfinance"].status == "missing_sdk"
    assert health_check("ecb")["ecb"].status == "ready"


def test_health_check_reports_missing_keys_without_exposing_values(monkeypatch) -> None:
    import opendata.core.config as config

    monkeypatch.delenv("FRED_API_KEY", raising=False)
    monkeypatch.delenv("FUYAO_API_KEY", raising=False)
    monkeypatch.setattr(
        config,
        "get_settings",
        lambda: SimpleNamespace(fred_api_key=None, fuyao_api_key=None),
    )

    fred = health_check("fred")["fred"]
    ths = health_check("ths")["ths"]
    eia_credential = get_provider("eia").credentials[0]

    assert fred.status == "missing_key"
    assert fred.missing_credentials == ("api_key",)
    assert ths.status == "missing_key"
    assert ths.missing_credentials == ("fuyao_api_key",)
    # eia's credential row carries a descriptive name since 360c61d; the environment
    # variable and the not-required-for-health claim are the load-bearing parts.
    assert eia_credential.name == "EIA API key (required for STEO API queries)"
    assert eia_credential.environment_variable == "EIA_API_KEY"
    assert not eia_credential.required_for_health
    assert "FRED_API_KEY" not in repr(fred)
    assert "FUYAO_API_KEY" not in repr(ths)


def test_provider_descriptor_rejects_non_local_fetcher_binding() -> None:
    provider = Provider(
        source="local",
        name="Local",
        description="A test provider.",
        fetcher_bindings=(LazyFetcherBinding("user_module.fetchers", "LocalFetcher"),),
    )
    with pytest.raises(ValueError, match="outside provider"):
        _ = provider.fetchers


def test_lazy_binding_rejects_capability_from_a_different_source(monkeypatch) -> None:
    import opendata.data.provider as provider_module
    from opendata.data.providers.ecb.models.cpi import EcbCpiFetcher

    monkeypatch.setattr(
        provider_module,
        "import_module",
        lambda _module: SimpleNamespace(EcbCpiFetcher=EcbCpiFetcher),
    )
    binding = LazyFetcherBinding("opendata.data.providers.local.models.fake", "EcbCpiFetcher")

    with pytest.raises(ValueError, match="capability source 'ecb'.*provider 'local'"):
        binding.load("local")
