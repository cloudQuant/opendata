"""Registry-derived OpenBB map projection tests."""

from __future__ import annotations

import socket
import sys
from dataclasses import replace
from typing import TYPE_CHECKING

import pytest

from opendata.data.openbb_map import OpenBBMapError, load_openbb_map
from opendata.data.provider import LazyFetcherBinding
from opendata.data.providers import register_providers
from opendata.data.providers.catalog import get_provider
from opendata.data.registry import ProviderRegistry

if TYPE_CHECKING:
    from pathlib import Path

CANONICAL_SCENARIOS = {
    ("fred", "FredSearch"): "FRED序列发现与研究参数选择",
    ("fred", "FredSeries"): "保留修订区间及请求变换上下文的宏观研究",
    ("fred", "SOFR"): "保留SOFR原始数值、修订区间与请求变换上下文的融资成本研究",
    ("fred", "SONIA"): "保留SONIA七测度、原始数值与修订区间的英镑融资成本研究",
    ("bls", "BlsSearch"): "BLS调查目录发现与原生维度查询",
    ("bls", "BlsSeries"): "保留原生期间及脚注的劳工统计研究",
    ("fmp", "EquityHistorical"): "保留FMP原生价格调整说明的日线研究",
    ("fmp", "EquityQuote"): "当前报价查询，不提供历史快照承诺",
    ("ecb", "CurrencyReferenceRates"): "保留ECB源维度与属性的欧元日参考汇率研究",
    ("ecb", "YieldCurve"): "保留ECB七维与源属性的日度收益率曲线研究",
    ("ecb", "BalanceOfPayments"): "保留ECB十七维与原月季度期间的国际收支研究",
    ("cboe", "AvailableIndices"): "指数目录发现：列出 cboe 发布的全部指数及其计算时段与延迟",
    ("cboe", "IndexConstituents"): "成分股快照：取出一只欧洲指数在源端发布的全部成分及其盘中报价",
    ("cboe", "IndexSearch"): "指数检索：在 cboe 美国指数目录中按代码或名称与描述筛选指数",
    (
        "federal_reserve",
        "MoneyMeasures",
    ): "货币供应量月表：取出 H.6 发布表在五行元数据之后的七个货币总量序列",
    (
        "federal_reserve",
        "TreasuryRates",
    ): "国债收益率日表：取出 H.15 发布表在五行元数据之后的十一个期限档",
}


@pytest.fixture
def registered_registry(monkeypatch: pytest.MonkeyPatch) -> ProviderRegistry:
    registry = ProviderRegistry()
    register_providers(registry)

    import opendata.data.registry as registry_module

    monkeypatch.setattr(registry_module, "get_registry", lambda: registry)
    return registry


def test_canonical_projection_matches_registry_binding_and_domain(
    registered_registry: ProviderRegistry,
) -> None:
    from opendata.data.domains import require_domain

    entries = load_openbb_map()
    descriptors = {
        (descriptor.source, descriptor.model): descriptor
        for descriptor in registered_registry.list_model_descriptors()
    }
    for identity, scenario in CANONICAL_SCENARIOS.items():
        source, model = identity
        descriptor = descriptors[identity]
        binding = get_provider(source).fetcher_dict[model]
        fetcher = registered_registry.resolve_model(source, model)
        domain = require_domain(descriptor.domain)
        mapped_entries = [
            entry for entry in entries if entry.model == model and entry.scenario == scenario
        ]

        assert len(mapped_entries) == 1
        assert model in binding.canonical_model_ids
        assert binding.scenario == scenario
        assert type(fetcher).__module__ == binding.module
        assert type(fetcher).__name__ == binding.class_name
        assert descriptor.domain == fetcher.capability.domain
        assert descriptor.capability_identity == (
            fetcher.capability.asset_class,
            fetcher.capability.domain,
            fetcher.capability.period,
            fetcher.capability.market,
            fetcher.capability.source,
        )
        assert len(mapped_entries[0].ours) == 1
        row = mapped_entries[0].ours[0]
        assert (row.provider, row.domain, row.contract, row.status) == (
            source,
            descriptor.domain,
            domain.contract,
            "verified" if descriptor.verified else "registered",
        )


def test_legacy_pending_aliases_and_old_scenarios_are_not_promoted(
    registered_registry: ProviderRegistry,
) -> None:
    entries = load_openbb_map()

    pending_rows = [
        row
        for entry in entries
        for row in entry.ours
        if row.provider == "ths" and row.domain == "financial_indicator"
    ]
    assert [(row.status, row.batch) for row in pending_rows] == [("pending", None)]
    assert any(
        entry.model == "TBD" and entry.scenario == "指数日线（业绩基准与指数增强回测 / dwd 指数域）"
        for entry in entries
    )
    assert not any(entry.model in {"FredCpi", "BlsObservation", "FmpQuote"} for entry in entries)
    assert len(registered_registry.list_model_descriptors()) == len(CANONICAL_SCENARIOS)


def test_registry_change_is_reflected_without_a_cached_projection(
    registered_registry: ProviderRegistry,
) -> None:
    initial = load_openbb_map()
    assert any(entry.model == "FredSearch" for entry in initial)

    search_fetcher = registered_registry.resolve_model("fred", "FredSearch")
    registered_registry.register_model("fred", "FredSearchAlias", search_fetcher)

    with pytest.raises(OpenBBMapError, match="no local binding"):
        load_openbb_map()


def test_legacy_status_is_derived_from_current_registry(
    registered_registry: ProviderRegistry,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def akshare_stock_daily_status() -> str:
        entry = next(
            entry
            for entry in load_openbb_map()
            if entry.model == "EquityHistorical"
            and entry.scenario == "行情日线（回测数据准备 / dwd 日线域）"
        )
        return next(
            row.status
            for row in entry.ours
            if row.provider == "akshare" and row.domain == "stock_daily"
        )

    capability = next(
        capability
        for capability in registered_registry.capabilities()
        if capability.source == "akshare" and capability.domain == "stock_daily"
    )
    assert akshare_stock_daily_status() == "registered"

    with monkeypatch.context() as patch:
        patch.setattr(capability, "verified", True)
        assert akshare_stock_daily_status() == "verified"

    assert capability.verified is False
    assert akshare_stock_daily_status() == "registered"


def test_shared_fetcher_capability_restores_across_two_registries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fetcher = get_provider("akshare").fetchers[0]
    first_registry = ProviderRegistry()
    second_registry = ProviderRegistry()
    first_registry.register(fetcher)
    second_registry.register(fetcher)
    first_capability = first_registry.capabilities()[0]
    second_capability = second_registry.capabilities()[0]
    original_verified = fetcher.capability.verified

    assert first_capability is second_capability is fetcher.capability

    with monkeypatch.context() as patch:
        patch.setattr(fetcher.capability, "verified", not original_verified)
        assert first_capability.verified is not original_verified
        assert second_capability.verified is not original_verified

    assert first_capability.verified is original_verified
    assert second_capability.verified is original_verified


def test_missing_canonical_scenario_fails_closed(
    registered_registry: ProviderRegistry,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import opendata.data.providers.catalog as catalog

    provider = get_provider("fred")
    bindings = tuple(
        replace(binding, scenario=None) if "FredSearch" in binding.canonical_model_ids else binding
        for binding in provider.fetcher_bindings
    )
    provider_without_scenario = replace(provider, fetcher_bindings=bindings)
    providers_by_source = dict(catalog._PROVIDERS_BY_SOURCE)
    providers_by_source["fred"] = provider_without_scenario
    monkeypatch.setattr(catalog, "_PROVIDERS_BY_SOURCE", providers_by_source)

    with pytest.raises(OpenBBMapError, match="has no scenario"):
        load_openbb_map()


def test_empty_registry_is_not_silently_populated(monkeypatch: pytest.MonkeyPatch) -> None:
    import opendata.data.registry as registry_module

    registry = ProviderRegistry()
    monkeypatch.setattr(registry_module, "get_registry", lambda: registry)

    with pytest.raises(OpenBBMapError, match="enabled legacy capability"):
        load_openbb_map()
    assert registry.capabilities() == []


def test_custom_v1_path_does_not_consult_registry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import opendata.data.registry as registry_module

    path = tmp_path / "custom.yaml"
    path.write_text(
        "version: 1\n"
        "entries:\n"
        "  - openbb: {model: TBD}\n"
        "    scenario: planned\n"
        "    ours:\n"
        "      - {provider: future, domain: not_registered, contract: Unknown, "
        "status: pending}\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        registry_module,
        "get_registry",
        lambda: pytest.fail("custom map loading must not consult the provider registry"),
    )

    entries = load_openbb_map(str(path))
    assert entries[0].ours[0].status == "pending"
    assert entries[0].ours[0].domain == "not_registered"


def test_cold_catalog_metadata_does_not_load_fetchers_or_open_sockets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import opendata.data.providers.catalog as catalog

    def deny(*args: object, **kwargs: object) -> None:
        raise AssertionError("metadata access attempted network I/O")

    def deny_fetcher_load(_binding: LazyFetcherBinding, _source: str) -> None:
        raise AssertionError("metadata access attempted to load a fetcher")

    modules_before = {
        name
        for name in sys.modules
        if name.startswith("opendata.data.providers.") and ".models." in name
    }
    monkeypatch.setattr(socket.socket, "connect", deny)
    monkeypatch.setattr(socket, "create_connection", deny)
    monkeypatch.setattr(LazyFetcherBinding, "load", deny_fetcher_load)

    for (source, model), scenario in CANONICAL_SCENARIOS.items():
        assert catalog.get_provider(source).fetcher_dict[model].scenario == scenario

    modules_after = {
        name
        for name in sys.modules
        if name.startswith("opendata.data.providers.") and ".models." in name
    }
    assert modules_after == modules_before


def test_legacy_custom_schema_remains_strict(tmp_path: Path) -> None:
    path = tmp_path / "custom.yaml"
    path.write_text("version: 2\nentries: []\n", encoding="utf-8")

    with pytest.raises(OpenBBMapError, match="version 1"):
        load_openbb_map(str(path))


def test_binding_scenario_validation_rejects_blank_values() -> None:
    with pytest.raises(ValueError, match="non-blank"):
        LazyFetcherBinding(
            "opendata.data.providers.fred.models.search",
            "FredSearchFetcher",
            scenario=" ",
        )
