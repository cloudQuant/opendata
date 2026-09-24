"""InterfaceLoader behaviour tests (B5.1 registry-only path).

Covers the registry-derived catalog with a mocked session, including
the idempotency and fail-closed branches of ``load_interfaces``.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from opendata.services.interface_loader import InterfaceLoader


@pytest.fixture
def loader():
    return InterfaceLoader()


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


class TestLoadInterfaces:
    async def test_empty_registry_is_an_empty_catalog(self, monkeypatch):
        from opendata.data import registry as registry_module
        from opendata.data.registry import ProviderRegistry

        monkeypatch.setattr(registry_module, "get_registry", ProviderRegistry)
        loader = InterfaceLoader()

        assert await loader.load_interfaces() == 0

    async def test_registry_scan_iterates_capabilities(self, monkeypatch, loader):
        from opendata.data import registry as registry_module

        registry = MagicMock()
        registry.capabilities.return_value = [
            _capability(),
            _capability(domain="economy_cpi"),
        ]
        monkeypatch.setattr(registry_module, "get_registry", lambda: registry)
        loader._load_capability_interface = AsyncMock(side_effect=[True, False])

        count = await loader.load_interfaces()

        assert count == 1
        assert loader._load_capability_interface.await_count == 2

    async def test_unknown_capability_domain_fails_closed(self, monkeypatch, loader):
        from opendata.data import registry as registry_module

        registry = MagicMock()
        registry.capabilities.return_value = [_capability(domain="not_a_domain")]
        monkeypatch.setattr(registry_module, "get_registry", lambda: registry)

        with pytest.raises(LookupError):
            await loader.load_interfaces()


class TestEnsureCategory:
    async def test_existing_category_is_returned(self, loader):
        from opendata.models.interface import InterfaceCategory

        existing = InterfaceCategory(name="equity", description="x", sort_order=1)
        db = AsyncMock()
        db.execute.return_value = MagicMock()
        db.execute.return_value.scalar_one_or_none.return_value = existing

        category = await loader._ensure_category("equity", db)

        assert category is existing
        db.add.assert_not_called()

    async def test_missing_category_is_created(self, loader):
        db = AsyncMock()
        db.execute.return_value = MagicMock()
        db.execute.return_value.scalar_one_or_none.return_value = None

        category = await loader._ensure_category("equity", db)

        assert category.name == "equity"
        db.add.assert_called_once()
        db.flush.assert_awaited_once()
