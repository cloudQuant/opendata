"""A1.7 routing seam tests.

The fetch dispatch in DataAcquisitionService routes through the
provider registry and falls back to the legacy direct call; the
interface loader honours the scan-source switch. Registry state is
per-test via monkeypatched get_registry - never the process singleton.
"""

from datetime import date
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pandas as pd
import pytest

from opendata.data import registry as registry_module
from opendata.data.capability import Capability
from opendata.data.models import Bar
from opendata.data.protocol import FetchContext, Fetcher, QueryParams
from opendata.data.registry import ProviderRegistry
from opendata.models.interface import DataInterface
from opendata.services.data_acquisition import DataAcquisitionService
from opendata.services.interface_loader import InterfaceLoader


class StubQuery(QueryParams):
    symbol: str


class StubFetcher(Fetcher[StubQuery, object]):
    """Returns its preconfigured result and records the call."""

    def __init__(self, capability: Capability, result: object):
        self.capability = capability
        self.result = result
        self.calls: list[tuple[str, dict[str, Any], FetchContext]] = []

    def transform_query(self, **kwargs: object) -> StubQuery:
        return StubQuery(**kwargs)  # type: ignore[arg-type]

    def extract_data(self, params: StubQuery, ctx: FetchContext) -> object:
        self.calls.append((params.symbol, dict(vars(params)), ctx))
        return "raw"

    def transform_data(self, raw: object, params: StubQuery) -> object:
        return self.result


def make_capability(domain: str = "stock_daily", verified: bool = True) -> Capability:
    return Capability(
        asset_class="equity",
        domain=domain,
        period="1D",
        market="cn",
        source="ths",
        verified=verified,
    )


def make_interface(name: str = "stock_daily") -> DataInterface:
    interface = MagicMock(spec=DataInterface)
    interface.name = name
    interface.id = 1
    return interface


def patch_registry(monkeypatch, *fetchers: StubFetcher) -> ProviderRegistry:
    """Install a private registry with the given fetchers."""
    registry = ProviderRegistry()
    for fetcher in fetchers:
        registry.register(fetcher)
    monkeypatch.setattr(registry_module, "get_registry", lambda: registry)
    return registry


class TestFetchRouting:
    async def test_unregistered_interface_falls_back_to_legacy(self, monkeypatch):
        patch_registry(monkeypatch)  # empty registry
        service = DataAcquisitionService()
        legacy = AsyncMock(return_value=pd.DataFrame({"a": [1]}))
        monkeypatch.setattr(service, "_call_akshare_function", legacy)

        result = await service._fetch_interface_data(make_interface("stock_zh_a_hist"), {})

        assert result is not None
        legacy.assert_awaited_once()

    async def test_registered_domain_routes_to_fetcher(self, monkeypatch):
        fetcher = StubFetcher(make_capability(), result=pd.DataFrame({"close": [1.0]}))
        patch_registry(monkeypatch, fetcher)
        service = DataAcquisitionService()
        legacy = AsyncMock(return_value=None)
        monkeypatch.setattr(service, "_call_akshare_function", legacy)

        result = await service._fetch_interface_data(
            make_interface("stock_daily"), {"symbol": "600519.SH", "bogus": None}
        )

        assert result is not None
        legacy.assert_not_awaited()
        assert [call[0] for call in fetcher.calls] == ["600519.SH"]

    async def test_contract_models_become_frame(self, monkeypatch):
        rows = [
            Bar(
                symbol="600519.SH",
                trade_date=date(2024, 6, 3),
                open=1700.0,
                high=1720.0,
                low=1695.0,
                close=1710.2,
                volume=100.0,
                amount=1000.0,
            )
        ]
        fetcher = StubFetcher(make_capability(), result=rows)
        patch_registry(monkeypatch, fetcher)
        service = DataAcquisitionService()

        frame = await service._fetch_interface_data(make_interface("stock_daily"), {"symbol": "x"})

        assert frame is not None
        assert frame["close"].tolist() == [1710.2]

    async def test_empty_model_sequence_becomes_none(self, monkeypatch):
        fetcher = StubFetcher(make_capability(), result=[])
        patch_registry(monkeypatch, fetcher)
        service = DataAcquisitionService()

        assert (
            await service._fetch_interface_data(make_interface("stock_daily"), {"symbol": "x"})
            is None
        )

    async def test_unverified_domain_not_auto_routed(self, monkeypatch):
        # verified=False never participates in auto routing (FR-3),
        # so the fetch falls back to the legacy call.
        fetcher = StubFetcher(make_capability(verified=False), result=pd.DataFrame())
        patch_registry(monkeypatch, fetcher)
        service = DataAcquisitionService()
        legacy = AsyncMock(return_value=pd.DataFrame({"a": [1]}))
        monkeypatch.setattr(service, "_call_akshare_function", legacy)

        await service._fetch_interface_data(make_interface("stock_daily"), {})

        legacy.assert_awaited_once()


class TestResolveDomain:
    def test_resolves_registered_domain(self):
        registry = ProviderRegistry()
        fetcher = StubFetcher(make_capability(), result=None)
        registry.register(fetcher)
        assert registry.resolve_domain("stock_daily") is fetcher

    def test_unknown_domain_raises(self):
        with pytest.raises(LookupError, match="no capability registered"):
            ProviderRegistry().resolve_domain("stock_daily")

    def test_explicit_source_via_domain(self):
        registry = ProviderRegistry()
        fetcher = StubFetcher(make_capability(), result=None)
        registry.register(fetcher)
        assert registry.resolve_domain("stock_daily", source="ths") is fetcher
        with pytest.raises(LookupError, match="no registered capability"):
            registry.resolve_domain("stock_daily", source="akshare")


class TestLoaderCatalog:
    async def test_registry_scan_empty_is_zero(self, monkeypatch):
        patch_registry(monkeypatch)
        loader = InterfaceLoader()
        assert await loader.load_interfaces() == 0

    async def test_registry_scan_creates_one_row_per_domain(self, monkeypatch, test_engine):
        from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

        monkeypatch.setattr(
            "opendata.services.interface_loader.async_session_maker",
            async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False),
        )
        ths = make_capability()
        registry = patch_registry(monkeypatch, StubFetcher(ths, result=None))
        registry.register(  # a second source of the same domain folds
            StubFetcher(
                Capability(
                    asset_class="equity",
                    domain="stock_daily",
                    period="1D",
                    market="cn",
                    source="akshare",
                    verified=True,
                ),
                result=None,
            )
        )
        loader = InterfaceLoader()
        assert await loader.load_interfaces() == 1
