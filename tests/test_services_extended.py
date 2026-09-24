"""
Services module extended tests.

Additional tests for service layer functionality.
"""

from unittest.mock import AsyncMock


class TestSchedulerServiceExtended:
    """Extended SchedulerService tests."""

    def test_scheduler_service_initialization(self):
        """Test SchedulerService can be initialized."""
        from opendata.services.scheduler_service import SchedulerService

        service = SchedulerService()
        assert service is not None

    def test_scheduler_service_has_methods(self):
        """Test SchedulerService has expected methods."""
        from opendata.services.scheduler_service import SchedulerService

        service = SchedulerService()

        # Check for key methods
        methods = ["start", "shutdown"]
        for method in methods:
            assert hasattr(service, method)


class TestScriptServiceExtended:
    """Extended ScriptService tests."""

    def test_script_service_initialization(self):
        """Test ScriptService can be initialized."""
        from opendata.services.script_service import ScriptService

        mock_db = AsyncMock()
        service = ScriptService(mock_db)
        assert service is not None


class TestExecutionServiceExtended:
    """Extended ExecutionService tests."""

    def test_execution_service_initialization(self):
        """Test ExecutionService can be initialized."""
        from opendata.services.execution_service import ExecutionService

        mock_db = AsyncMock()
        service = ExecutionService(mock_db)
        assert service is not None


class TestDataAcquisitionExtended:
    """Extended DataAcquisitionService tests."""

    def test_data_acquisition_initialization(self):
        """Test DataAcquisitionService can be initialized."""
        from opendata.services.data_acquisition import DataAcquisitionService

        service = DataAcquisitionService()
        assert service is not None


class TestServiceInitialization:
    """Test service initialization patterns."""

    def test_services_module_exports(self):
        """Test services module exports all expected services."""
        from opendata.services import (
            data_acquisition,
            execution_service,
            interface_loader,
            retry_service,
            scheduler_service,
            script_service,
        )

        assert script_service is not None
        assert execution_service is not None
        assert scheduler_service is not None
        assert data_acquisition is not None
        assert interface_loader is not None
        assert retry_service is not None


class TestRetryServiceExtended:
    """Extended RetryService tests."""

    def test_retry_service_constants(self):
        """Test RetryService constants."""
        from opendata.services.retry_service import RetryService

        assert hasattr(RetryService, "BASE_RETRY_DELAY")
        assert hasattr(RetryService, "MAX_RETRY_DELAY")
        assert RetryService.BASE_RETRY_DELAY > 0
        assert RetryService.MAX_RETRY_DELAY > 0

    def test_retry_service_initialization(self):
        """Test RetryService can be initialized."""
        from opendata.services.retry_service import RetryService

        mock_db = AsyncMock()
        service = RetryService(mock_db)
        assert service is not None

    def test_retry_service_delay_calculation(self):
        """Test retry delay calculation."""
        from opendata.services.retry_service import RetryService

        mock_db = AsyncMock()
        service = RetryService(mock_db)

        # Test exponential backoff
        delay1 = service.calculate_retry_delay(0)
        delay2 = service.calculate_retry_delay(1)
        delay3 = service.calculate_retry_delay(2)

        assert delay2 > delay1  # Should grow exponentially
        assert delay3 > delay2

    def test_retry_service_delay_capped(self):
        """Test retry delay is capped at MAX."""
        from opendata.services.retry_service import RetryService

        mock_db = AsyncMock()
        service = RetryService(mock_db)

        # Very high retry count should still be capped
        delay = service.calculate_retry_delay(100)
        assert delay <= RetryService.MAX_RETRY_DELAY


class TestTaskScheduler:
    """Test task scheduler."""

    def test_scheduler_service_class_exists(self):
        """Test SchedulerService class exists."""
        from opendata.services.scheduler_service import SchedulerService

        assert SchedulerService is not None

    def test_scheduler_service_initialization(self):
        """Test task scheduler can be initialized."""
        from opendata.services.scheduler_service import SchedulerService

        scheduler = SchedulerService()
        assert scheduler is not None


class TestInterfaceLoaderService:
    """Test InterfaceLoader service."""

    def test_interface_loader_initialization(self):
        """Test InterfaceLoader can be initialized."""
        from opendata.services.interface_loader import InterfaceLoader

        loader = InterfaceLoader()
        assert loader is not None

    def test_interface_loader_is_registry_backed(self):
        """The legacy reflection scan was removed (B5.1)."""
        from opendata.services.interface_loader import InterfaceLoader

        assert not hasattr(InterfaceLoader, "CATEGORY_MAPPING")


class TestProviderIntegration:
    """Test provider integration."""

    def test_akshare_provider_exists(self):
        """Test akshare provider can be imported."""
        from opendata.data_fetch.providers.akshare_provider import AkshareProvider

        assert AkshareProvider is not None

    def test_akshare_provider_initialization(self):
        """Test akshare provider can be instantiated."""
        from opendata.data_fetch.providers.akshare_provider import AkshareProvider

        provider = AkshareProvider()
        assert provider is not None


class TestServicesIntegration:
    """Test services integration."""

    def test_all_services_can_be_imported(self):
        """Test all service modules can be imported."""
        from opendata.services import (
            data_acquisition,
            execution_service,
            scheduler_service,
            script_service,
        )

        # All should be importable
        assert script_service is not None
        assert execution_service is not None
        assert scheduler_service is not None
        assert data_acquisition is not None


class TestSchedulerModule:
    """Test scheduler module."""

    def test_scheduler_module_exists(self):
        """Test scheduler module exists."""
        from opendata.services import scheduler

        assert scheduler is not None
