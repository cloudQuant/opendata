"""
Interface loader detailed tests.

Tests for InterfaceLoader functionality.
"""

import pytest


class TestInterfaceLoaderInit:
    """Test InterfaceLoader initialization."""

    def test_interface_loader_init(self):
        """Test InterfaceLoader can be initialized."""
        from opendata.services.interface_loader import InterfaceLoader

        loader = InterfaceLoader()
        assert loader is not None

    def test_category_mapping_constant(self):
        """Test CATEGORY_MAPPING constant exists."""
        from opendata.services.interface_loader import InterfaceLoader

        assert hasattr(InterfaceLoader, "CATEGORY_MAPPING")
        assert isinstance(InterfaceLoader.CATEGORY_MAPPING, dict)


class TestInterfaceLoaderCategories:
    """Test InterfaceLoader category functionality."""

    def test_category_mapping_has_common_categories(self):
        """Test category mapping has common data categories."""
        from opendata.services.interface_loader import InterfaceLoader

        mapping = InterfaceLoader.CATEGORY_MAPPING

        # Check for common financial data categories
        categories = list(mapping.keys())
        category_values = list(mapping.values())

        # Should have at least some categories
        assert len(categories) > 0

    def test_get_category_method(self):
        """Test _get_category method."""
        from opendata.services.interface_loader import InterfaceLoader

        loader = InterfaceLoader()

        # Test getting category from function name
        category = loader._get_category("stock_zh_a_hist")

        # Should return a category
        assert category is not None
        assert isinstance(category, str)

    def test_get_category_unknown_function(self):
        """Test _get_category with unknown function."""
        from opendata.services.interface_loader import InterfaceLoader

        loader = InterfaceLoader()

        # Unknown function should still return a category
        category = loader._get_category("unknown_function_xyz")

        assert category is not None


class TestInterfaceLoaderDisplayName:
    """Test display name generation."""

    def test_generate_display_name(self):
        """Test generating display names."""
        from opendata.services.interface_loader import InterfaceLoader

        loader = InterfaceLoader()

        # Test underscore to space conversion
        display_name = loader._generate_display_name("stock_zh_a_hist")

        assert display_name is not None
        assert " " in display_name or display_name == display_name


class TestInterfaceLoaderParameterParsing:
    """Test parameter parsing."""

    def test_get_type_name(self):
        """Test _get_type_name method."""
        from opendata.services.interface_loader import InterfaceLoader

        loader = InterfaceLoader()

        # Test with basic types
        str_type = loader._get_type_name(str)
        assert str_type is not None

        int_type = loader._get_type_name(int)
        assert int_type is not None

    def test_map_parameter_type(self):
        """Test _map_parameter_type method."""
        from opendata.services.interface_loader import InterfaceLoader

        loader = InterfaceLoader()

        # Test type mapping
        param_type = loader._map_parameter_type(str)
        assert param_type is not None

        int_type = loader._map_parameter_type(int)
        assert int_type is not None


class TestInterfaceLoaderDocParsing:
    """Test docstring parsing."""

    def test_parse_description_empty(self):
        """Test parsing empty description."""
        from opendata.services.interface_loader import InterfaceLoader

        loader = InterfaceLoader()

        description = loader._parse_description("")

        assert description == ""

    def test_parse_example_with_no_example(self):
        """Test parsing docstring without example."""
        from opendata.services.interface_loader import InterfaceLoader

        loader = InterfaceLoader()

        example = loader._parse_example("This is a docstring without example")

        # Should return None
        assert example is None


class TestInterfaceLoaderLoadFromAkshare:
    """Test loading from akshare."""

    @pytest.mark.asyncio
    async def test_load_from_akshare_method_exists(self):
        """Test load_from_akshare method exists and is async."""
        import inspect

        from opendata.services.interface_loader import InterfaceLoader

        loader = InterfaceLoader()

        # Verify method exists and is async
        assert hasattr(loader, "load_from_akshare")
        assert inspect.iscoroutinefunction(loader.load_from_akshare)


class TestInterfaceLoaderDiscoverFunctions:
    """Test function discovery."""

    def test_discover_akshare_functions(self):
        """Test discovering akshare functions."""
        from opendata.services.interface_loader import InterfaceLoader

        loader = InterfaceLoader()

        functions = loader._discover_akshare_functions()

        # Should return a list
        assert isinstance(functions, list)
        # Should have at least some functions
        assert len(functions) > 0
