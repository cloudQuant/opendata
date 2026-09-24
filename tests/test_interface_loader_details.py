"""Interface loader tests (B5.1: registry-only catalog, FR-17).

The legacy reflection scan and its parsing helpers were removed with
the compatibility switch; what remains is the registry-derived catalog,
so the tests cover its idempotency and fail-closed behaviour instead.
"""

from __future__ import annotations

import pytest

from opendata.data.registry import ProviderRegistry
from opendata.services.interface_loader import InterfaceLoader


def _capability(**overrides):
    from opendata.data.capability import Capability

    defaults = {
        "asset_class": "equity",
        "domain": "stock_daily",
        "period": "1D",
        "market": "cn",
        "source": "ths",
        "verified": True,
    }
    defaults.update(overrides)
    return Capability(**defaults)


class TestLoaderInit:
    def test_interface_loader_init(self):
        loader = InterfaceLoader()
        assert loader is not None

    def test_loader_is_a_singleton(self):
        from opendata.services.interface_loader import interface_loader

        assert isinstance(interface_loader, InterfaceLoader)


class TestLoadCapabilityInterface:
    async def test_creates_one_row_per_domain(self, test_db):
        loader = InterfaceLoader()

        first = await loader._load_capability_interface(_capability(), test_db)
        second = await loader._load_capability_interface(_capability(source="akshare"), test_db)

        assert first is True
        assert second is False  # same domain folds into the existing row

    async def test_unknown_domain_fails_closed(self, test_db):
        loader = InterfaceLoader()

        with pytest.raises(LookupError):
            await loader._load_capability_interface(_capability(domain="not_a_domain"), test_db)

    async def test_category_is_created_from_the_asset_class(self, test_db):
        from sqlalchemy import select

        from opendata.models.interface import InterfaceCategory

        loader = InterfaceLoader()
        await loader._load_capability_interface(_capability(), test_db)

        result = await test_db.execute(
            select(InterfaceCategory).where(InterfaceCategory.name == "equity")
        )
        assert result.scalar_one_or_none() is not None


class TestLoadInterfaces:
    async def test_empty_registry_is_an_empty_catalog(self, monkeypatch):
        from opendata.data import registry as registry_module

        registry = ProviderRegistry()
        monkeypatch.setattr(registry_module, "get_registry", lambda: registry)
        loader = InterfaceLoader()

        assert await loader.load_interfaces() == 0

    async def test_load_interfaces_commits_the_catalog(self, monkeypatch, test_engine):
        from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

        from opendata.data import registry as registry_module

        monkeypatch.setattr(
            "opendata.services.interface_loader.async_session_maker",
            async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False),
        )
        registry = ProviderRegistry()
        registry.register(StubFetcher())
        monkeypatch.setattr(registry_module, "get_registry", lambda: registry)
        loader = InterfaceLoader()

        count = await loader.load_interfaces()

        assert count == 1


class StubFetcher:
    """Minimal capability carrier for the registry tests."""

    capability = _capability()
