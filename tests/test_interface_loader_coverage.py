"""
Tests for InterfaceLoader covering _load_interface, _ensure_categories,
_parse_example, _get_category, _parse_parameters, _create_parameter_definitions.
"""

import inspect
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from opendata.services.interface_loader import InterfaceLoader


@pytest.fixture
def loader():
    return InterfaceLoader()


class TestEnsureCategories:
    @pytest.mark.asyncio
    async def test_creates_missing_categories(self, loader):
        mock_db = AsyncMock()
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None  # category doesn't exist
        mock_db.execute.return_value = mock_result

        await loader._ensure_categories(mock_db)
        # Should add categories and commit
        assert mock_db.add.call_count >= 1
        mock_db.commit.assert_called()

    @pytest.mark.asyncio
    async def test_skips_existing_categories(self, loader):
        mock_db = AsyncMock()
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = MagicMock()  # exists
        mock_db.execute.return_value = mock_result

        await loader._ensure_categories(mock_db)
        mock_db.add.assert_not_called()


class TestLoadInterface:
    @pytest.mark.asyncio
    async def test_already_exists(self, loader):
        mock_db = AsyncMock()
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = MagicMock()  # already loaded
        mock_db.execute.return_value = mock_result

        def sample_func(x: str, y: int = 5):
            """Sample function."""

        await loader._load_interface("sample_func", sample_func, mock_db)
        mock_db.add.assert_not_called()

    @pytest.mark.asyncio
    async def test_new_with_category(self, loader):
        mock_db = AsyncMock()
        # First call: interface not found, second: category found
        results = [MagicMock(), MagicMock()]
        results[0].scalar_one_or_none.return_value = None  # not loaded yet
        mock_category = MagicMock()
        mock_category.id = 1
        results[1].scalar_one_or_none.return_value = mock_category
        mock_db.execute.side_effect = results

        def stock_data(symbol: str = "000001"):
            """Get stock data."""

        await loader._load_interface("stock_data", stock_data, mock_db)
        mock_db.add.assert_called()

    @pytest.mark.asyncio
    async def test_new_fallback_category(self, loader):
        mock_db = AsyncMock()
        # interface not found, category not found, fallback category found
        results = [MagicMock(), MagicMock(), MagicMock()]
        results[0].scalar_one_or_none.return_value = None  # not loaded
        results[1].scalar_one_or_none.return_value = None  # category not found
        mock_category = MagicMock()
        mock_category.id = 1
        results[2].scalar_one_or_none.return_value = mock_category  # fallback "stock"
        mock_db.execute.side_effect = results

        def custom_func():
            pass

        await loader._load_interface("custom_func", custom_func, mock_db)
        mock_db.add.assert_called()

    @pytest.mark.asyncio
    async def test_no_signature(self, loader):
        mock_db = AsyncMock()
        results = [MagicMock(), MagicMock()]
        results[0].scalar_one_or_none.return_value = None
        mock_category = MagicMock()
        mock_category.id = 1
        results[1].scalar_one_or_none.return_value = mock_category
        mock_db.execute.side_effect = results

        # Use a builtin that may not have inspectable signature
        func = MagicMock()
        func.__doc__ = "Test doc"
        func.__name__ = "test_func"

        with patch("opendata.services.interface_loader.inspect.signature", side_effect=ValueError):
            await loader._load_interface("test_func", func, mock_db)
        mock_db.add.assert_called()


class TestGetCategory:
    def test_stock_prefix(self, loader):
        assert loader._get_category("stock_zh_a_hist") == "stock"

    def test_fund_prefix(self, loader):
        assert loader._get_category("fund_etf_hist") == "fund"

    def test_default(self, loader):
        assert loader._get_category("random_function") == "stock"


class TestParseExample:
    def test_no_docstring(self, loader):
        assert loader._parse_example("") is None
        assert loader._parse_example(None) is None

    def test_with_example(self, loader):
        doc = "Description\nExample\n  stock_data(symbol='000001')"
        result = loader._parse_example(doc)
        assert result is not None

    def test_no_example(self, loader):
        doc = "Just a description"
        assert loader._parse_example(doc) is None


class TestParseParameters:
    def test_with_params(self, loader):
        def func(x: str, y: int = 5):
            pass

        sig = inspect.signature(func)
        result = loader._parse_parameters(sig, "")
        assert "x" in result
        assert result["x"]["required"] is True
        assert "y" in result
        assert result["y"]["required"] is False

    def test_skips_self_kwargs(self, loader):
        def func(self, x: str, **kwargs):
            pass

        sig = inspect.signature(func)
        result = loader._parse_parameters(sig, "")
        assert "self" not in result
        assert "kwargs" not in result
        assert "x" in result


class TestGetTypeName:
    def test_types(self, loader):
        assert loader._get_type_name(str) == "string"
        assert loader._get_type_name(int) == "integer"
        assert loader._get_type_name(float) == "float"
        assert loader._get_type_name(bool) == "boolean"
        assert loader._get_type_name(inspect.Parameter.empty) == "string"
        assert loader._get_type_name(list) == "string"


class TestMapParameterType:
    def test_types(self, loader):
        from opendata.models.interface import ParameterType

        assert loader._map_parameter_type(str) == ParameterType.STRING
        assert loader._map_parameter_type(int) == ParameterType.INTEGER
        assert loader._map_parameter_type(float) == ParameterType.FLOAT
        assert loader._map_parameter_type(bool) == ParameterType.BOOLEAN
        assert loader._map_parameter_type(list) == ParameterType.STRING
        assert loader._map_parameter_type(inspect.Parameter.empty) == ParameterType.STRING


class TestLoadFromAkshare:
    @pytest.mark.asyncio
    async def test_load_success(self, loader):
        mock_session = AsyncMock()
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        mock_session.execute.return_value = mock_result

        with patch("opendata.services.interface_loader.async_session_maker") as mock_sm:
            mock_sm.return_value.__aenter__ = AsyncMock(return_value=mock_session)
            mock_sm.return_value.__aexit__ = AsyncMock(return_value=False)

            with (
                patch.object(loader, "_ensure_categories", new_callable=AsyncMock),
                patch.object(
                    loader, "_discover_akshare_functions", return_value=[("func1", lambda: None)]
                ),
                patch.object(loader, "_load_interface", new_callable=AsyncMock),
            ):
                count = await loader.load_from_akshare()
            assert count == 1

    @pytest.mark.asyncio
    async def test_load_with_error(self, loader):
        mock_session = AsyncMock()

        with patch("opendata.services.interface_loader.async_session_maker") as mock_sm:
            mock_sm.return_value.__aenter__ = AsyncMock(return_value=mock_session)
            mock_sm.return_value.__aexit__ = AsyncMock(return_value=False)

            with (
                patch.object(loader, "_ensure_categories", new_callable=AsyncMock),
                patch.object(
                    loader, "_discover_akshare_functions", return_value=[("func1", lambda: None)]
                ),
                patch.object(
                    loader,
                    "_load_interface",
                    new_callable=AsyncMock,
                    side_effect=RuntimeError("fail"),
                ),
            ):
                count = await loader.load_from_akshare()
            assert count == 0
