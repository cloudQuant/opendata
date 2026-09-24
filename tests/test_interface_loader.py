"""
Interface loader service tests (B5.1: registry-only catalog).

The legacy reflection scan over the ported package was removed with
the compatibility switch (FR-17), so the contract under test is the
registry-derived catalog: one row per domain, idempotent, fail closed
on unregistered domains. The behavioural coverage lives in
``test_routing_seam.py`` and ``test_interface_loader_details.py``;
this file pins the surface that survived the removal.
"""

from opendata.services.interface_loader import InterfaceLoader


class TestInterfaceLoader:
    """Test InterfaceLoader class."""

    def test_interface_loader_exists(self):
        """Test InterfaceLoader class exists."""
        assert InterfaceLoader is not None

    def test_loader_is_a_singleton(self):
        """Test the module-level singleton."""
        from opendata.services.interface_loader import interface_loader

        assert isinstance(interface_loader, InterfaceLoader)

    def test_load_interfaces_is_async(self):
        """Test load_interfaces is an async method."""
        import inspect

        assert inspect.iscoroutinefunction(InterfaceLoader.load_interfaces)

    def test_legacy_reflection_surface_is_gone(self):
        """The akshare reflection helpers were removed (B5.1).

        The catalog is the registry - keeping the old discovery surface
        around would be a second, drifting inventory.
        """
        loader = InterfaceLoader()

        assert not hasattr(loader, "load_from_akshare")
        assert not hasattr(loader, "_discover_akshare_functions")
        assert not hasattr(loader, "_load_interface")
        assert not hasattr(InterfaceLoader, "CATEGORY_MAPPING")
