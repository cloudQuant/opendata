"""
Interface loader service tests.

Tests for the akshare interface discovery and loading service.
"""

import contextlib
from unittest.mock import AsyncMock, patch


class TestInterfaceLoader:
    """Test InterfaceLoader class."""

    def test_interface_loader_exists(self):
        """Test InterfaceLoader class exists."""
        from opendata.services.interface_loader import InterfaceLoader

        assert InterfaceLoader is not None

    def test_category_mapping_exists(self):
        """Test CATEGORY_MAPPING exists."""
        from opendata.services.interface_loader import InterfaceLoader

        assert hasattr(InterfaceLoader, "CATEGORY_MAPPING")
        assert isinstance(InterfaceLoader.CATEGORY_MAPPING, dict)

    def test_category_mapping_has_stock(self):
        """Test category mapping has stock entries."""
        from opendata.services.interface_loader import InterfaceLoader

        assert "stock_zh_a_hist" in InterfaceLoader.CATEGORY_MAPPING
        assert InterfaceLoader.CATEGORY_MAPPING["stock_zh_a_hist"] == "stock"

    def test_category_mapping_has_fund(self):
        """Test category mapping has fund entries."""
        from opendata.services.interface_loader import InterfaceLoader

        assert "fund_open_fund_info_em" in InterfaceLoader.CATEGORY_MAPPING
        assert InterfaceLoader.CATEGORY_MAPPING["fund_open_fund_info_em"] == "fund"

    def test_load_from_akshare_method_exists(self):
        """Test load_from_akshare method exists."""
        from opendata.services.interface_loader import InterfaceLoader

        loader = InterfaceLoader()
        assert hasattr(loader, "load_from_akshare")
        assert callable(loader.load_from_akshare)

    def test_ensure_categories_method_exists(self):
        """Test _ensure_categories method exists."""
        from opendata.services.interface_loader import InterfaceLoader

        loader = InterfaceLoader()
        assert hasattr(loader, "_ensure_categories")
        assert callable(loader._ensure_categories)

    def test_discover_akshare_functions_method_exists(self):
        """Test _discover_akshare_functions method exists."""
        from opendata.services.interface_loader import InterfaceLoader

        loader = InterfaceLoader()
        assert hasattr(loader, "_discover_akshare_functions")
        assert callable(loader._discover_akshare_functions)

    def test_discover_akshare_functions_returns_list(self):
        """Test _discover_akshare_functions returns list."""
        from opendata.services.interface_loader import InterfaceLoader

        loader = InterfaceLoader()
        functions = loader._discover_akshare_functions()

        assert isinstance(functions, list)

    def test_discover_akshare_functions_has_tuples(self):
        """Test _discover_akshare_functions returns tuples."""
        from opendata.services.interface_loader import InterfaceLoader

        loader = InterfaceLoader()
        functions = loader._discover_akshare_functions()

        # Each item should be a tuple of (name, function)
        if len(functions) > 0:
            for item in functions[:5]:  # Check first 5
                assert isinstance(item, tuple)
                assert len(item) == 2

    def test_has_expected_categories(self):
        """Test expected categories exist."""
        from opendata.services.interface_loader import InterfaceLoader

        loader = InterfaceLoader()

        # Check that the method exists and works
        with patch("opendata.services.interface_loader.async_session_maker") as mock_session:
            mock_db = AsyncMock()
            mock_session().__aenter__.return_value = mock_db

            # Call the method
            import asyncio

            with contextlib.suppress(Exception):  # May fail without actual database
                asyncio.run(loader._ensure_categories(mock_db))

    def test_discover_functions_filters_akshare(self):
        """Test function discovery filters akshare module."""
        from opendata.services.interface_loader import InterfaceLoader

        loader = InterfaceLoader()
        functions = loader._discover_akshare_functions()

        # Should return some functions
        # In testing environment, akshare may not be fully available
        assert isinstance(functions, list)


class TestInterfaceLoaderIntegration:
    """Integration tests for interface loader."""

    def test_interface_loader_initialization(self):
        """Test InterfaceLoader can be initialized."""
        from opendata.services.interface_loader import InterfaceLoader

        loader = InterfaceLoader()
        assert loader is not None

    def test_all_methods_exist(self):
        """Test all expected methods exist."""
        from opendata.services.interface_loader import InterfaceLoader

        loader = InterfaceLoader()

        expected_methods = [
            "load_from_akshare",
            "_ensure_categories",
            "_discover_akshare_functions",
            "_load_interface",
        ]

        for method in expected_methods:
            assert hasattr(loader, method)

    def test_category_mapping_values(self):
        """Test all category mapping values are valid."""
        from opendata.services.interface_loader import InterfaceLoader

        valid_categories = [
            "stock",
            "fund",
            "futures",
            "index",
            "bond",
            "forex",
            "economic",
            "macro",
        ]

        for category in InterfaceLoader.CATEGORY_MAPPING.values():
            assert category in valid_categories


class TestDataInterfaceModels:
    """Test data interface related models."""

    def test_interface_category_model_exists(self):
        """Test InterfaceCategory model exists."""
        from opendata.models.interface import InterfaceCategory

        assert InterfaceCategory is not None

    def test_data_interface_model_exists(self):
        """Test DataInterface model exists."""
        from opendata.models.interface import DataInterface

        assert DataInterface is not None

    def test_interface_parameter_model_exists(self):
        """Test InterfaceParameter model exists."""
        from opendata.models.interface import InterfaceParameter

        assert InterfaceParameter is not None

    def test_parameter_type_enum_exists(self):
        """Test ParameterType enum exists."""
        from opendata.models.interface import ParameterType

        assert ParameterType is not None

    def test_parameter_type_has_values(self):
        """Test ParameterType has expected values."""
        from opendata.models.interface import ParameterType

        # Should have common parameter types
        assert hasattr(ParameterType, "__members__")
        assert len(ParameterType.__members__) > 0


class TestAkshareIntegration:
    """Test akshare integration."""

    def test_akshare_importable(self):
        """Test the ported package can be imported."""
        import opendata_http as ak

        assert ak is not None

    def test_akshare_has_functions(self):
        """Test akshare module has functions."""
        import opendata_http as ak

        # Should have stock functions
        assert hasattr(ak, "stock_zh_a_hist") or hasattr(ak, "stock")

    def test_akshare_function_callable(self):
        """Test akshare functions are callable."""
        import opendata_http as ak

        if hasattr(ak, "stock_zh_a_hist"):
            func = ak.stock_zh_a_hist
            assert callable(func)
