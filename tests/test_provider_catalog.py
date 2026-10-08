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
from opendata.data.providers.catalog import (
    get_provider,
    health_check,
    list_providers,
    register_providers,
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


def test_catalog_contains_34_sources_and_only_46_fetcher_bindings() -> None:
    providers = list_providers()

    assert len(providers) == 34
    assert {provider.source for provider in providers} == EXPECTED_SOURCES
    assert {provider.source for provider in providers if provider.is_implemented} == {
        "akshare",
        "bls",
        "cboe",
        "ecb",
        "fred",
        "fmp",
        "imf",
        "oecd",
        "ths",
        "yfinance",
    }
    assert sum(len(provider.fetcher_bindings) for provider in providers) == 46
    implemented = [provider for provider in providers if provider.is_implemented]
    assert all(
        provider.website and provider.website.startswith("https://") for provider in implemented
    )

    reserved = [provider for provider in providers if not provider.is_implemented]
    assert len(reserved) == 24
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


def test_registration_and_legacy_fetchers_project_the_same_44_capabilities() -> None:
    registry = ProviderRegistry()
    registered = register_providers(registry)
    source_fetchers = []
    expected_counts = {
        "akshare": 10,
        "ecb": 6,
        "fred": 7,
        "imf": 3,
        "oecd": 2,
        "ths": 11,
        "yfinance": 1,
        "bls": 2,
        "fmp": 2,
    }

    for source, count in expected_counts.items():
        registration = importlib.import_module(f"opendata.data.providers.{source}.registration")
        package = importlib.import_module(f"opendata.data.providers.{source}")
        provider = get_provider(source)
        fetchers = registration.FETCHERS
        assert len(fetchers) == count
        assert len(provider.fetcher_dict) == count
        assert tuple(provider.fetcher_dict.values()) == provider.fetcher_bindings
        assert package.register is registration.register
        if source != "akshare":
            assert fetchers == package.FETCHERS
        source_fetchers.extend(fetchers)

    expected = {_capability_identity(fetcher.capability) for fetcher in source_fetchers}
    actual = {_capability_identity(capability) for capability in registry.capabilities()}
    expected_registration_order = [
        source for source, count in expected_counts.items() for _ in range(count)
    ]
    assert len(registered) == 44
    assert len(actual) == 44
    assert actual == expected
    assert [capability.source for capability in registered] == expected_registration_order
    assert register_providers(registry) == []


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
    assert eia_credential.name == "api_key"
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
